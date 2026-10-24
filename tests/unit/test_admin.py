import json
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from pr_review.api.app import create_app
from pr_review.core.settings import Settings
from pr_review.pipeline import cli, state
from pr_review.storage.models import Base, Event, Job, LlmCall, StageAttempt

TOKEN = "admin-token-for-tests"
H = {"Authorization": f"Bearer {TOKEN}"}


class Enq:
    def __init__(self) -> None:
        self.jobs: list[uuid.UUID] = []

    async def enqueue(self, job_id: uuid.UUID) -> None:
        self.jobs.append(job_id)


def make_settings(token: str | None = TOKEN) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        github_app_id=1,
        github_private_key="pk",  # type: ignore[arg-type]
        github_webhook_secret="whsec-test-secret",  # type: ignore[arg-type]
        database_url="postgresql://u:p@h/db",  # type: ignore[arg-type]
        redis_url="redis://h/0",  # type: ignore[arg-type]
        admin_token=token,  # type: ignore[arg-type]
    )


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(engine)
    engine.dispose()


def mkjob(f: sessionmaker[Session], status: str = "queued", reason: str | None = None) -> uuid.UUID:
    with f.begin() as s:
        j = Job(
            repo="o/r",
            pr_number=1,
            head_sha=uuid.uuid4().hex,
            installation_id=1,
            status=status,
            dead_letter_reason=reason,
        )
        s.add(j)
        s.flush()
        s.add(StageAttempt(job_id=j.id, stage="fetch", attempt_no=1, status="failed", error="boom"))
        s.add(
            LlmCall(
                job_id=j.id,
                model="m1",
                prompt_tokens=10,
                completion_tokens=5,
                cost_usd=Decimal("0.25"),
                outcome="ok",
            )
        )
        return j.id


@pytest.fixture
def enq() -> Enq:
    return Enq()


@pytest.fixture
def client(factory: sessionmaker[Session], enq: Enq) -> TestClient:
    app = create_app(make_settings())
    app.state.session_factory = factory
    app.state.enqueuer = enq
    return TestClient(app)


def test_unset_token_fails_closed(factory: sessionmaker[Session]) -> None:
    app = create_app(make_settings(None))
    app.state.session_factory = factory
    c = TestClient(app)
    assert c.get("/admin/jobs", headers=H).status_code == 503
    assert c.get("/admin/jobs").status_code == 503


@pytest.mark.parametrize("hdr", [{}, {"Authorization": "Bearer nope"}, {"Authorization": TOKEN}])
def test_bad_credentials_401(client: TestClient, hdr: dict[str, str]) -> None:
    r = client.get("/admin/jobs", headers=hdr)
    assert r.status_code == 401
    assert TOKEN not in r.text


def test_jobs_and_detail(client: TestClient, factory: sessionmaker[Session]) -> None:
    jid = mkjob(factory)
    assert [j["id"] for j in client.get("/admin/jobs", headers=H).json()["jobs"]] == [str(jid)]
    assert client.get("/admin/jobs?status=running", headers=H).json() == {"jobs": []}
    d = client.get(f"/admin/jobs/{jid}", headers=H).json()
    assert d["attempts"][0]["stage"] == "fetch"
    assert client.get(f"/admin/jobs/{uuid.uuid4()}", headers=H).status_code == 404
    assert client.get("/admin/jobs?limit=0", headers=H).status_code == 422


def test_timeline_only_with_token(client: TestClient, factory: sessionmaker[Session]) -> None:
    jid = mkjob(factory)
    assert client.get(f"/jobs/{jid}/timeline").status_code in (401, 404)
    assert client.get(f"/jobs/{jid}/timeline", headers=H).status_code == 404
    assert client.get(f"/admin/jobs/{jid}/timeline").status_code == 401
    r = client.get(f"/admin/jobs/{jid}/timeline", headers=H)
    assert r.status_code == 200 and r.json()["job_id"] == str(jid)
    assert client.get(f"/admin/jobs/{uuid.uuid4()}/timeline", headers=H).status_code == 404
    assert client.get("/healthz").status_code == 200


def test_dlq_list_inspect_requeue(
    client: TestClient, factory: sessionmaker[Session], enq: Enq
) -> None:
    dead = mkjob(factory, "dead_letter", "stage fetch exhausted")
    live = mkjob(factory)
    assert [j["id"] for j in client.get("/admin/dlq", headers=H).json()["jobs"]] == [str(dead)]
    d = client.get(f"/admin/dlq/{dead}", headers=H).json()
    assert d["dead_letter_reason"] == "stage fetch exhausted" and d["attempts"]
    assert client.get(f"/admin/dlq/{live}", headers=H).status_code == 404
    assert client.post(f"/admin/dlq/{dead}/requeue").status_code == 401
    assert client.post(f"/admin/dlq/{live}/requeue", headers=H).status_code == 404
    assert client.post(f"/admin/dlq/{dead}/requeue", headers=H).json()["status"] == "queued"
    assert enq.jobs == [dead]
    assert client.post(f"/admin/dlq/{dead}/requeue", headers=H).status_code == 404
    with factory() as s:
        assert s.get(Job, dead).status == state.QUEUED  # type: ignore[union-attr]
        assert s.scalar(select(Event.id).where(Event.job_id == dead)) is not None


def test_costs(client: TestClient, factory: sessionmaker[Session]) -> None:
    mkjob(factory)
    mkjob(factory)
    (row,) = client.get("/admin/costs", headers=H).json()["costs"]
    assert row["model"] == "m1" and row["calls"] == 2 and Decimal(row["cost_usd"]) == Decimal("0.5")
    assert row["prompt_tokens"] == 20


@pytest.fixture
def dburl(tmp_path: Path) -> Iterator[str]:
    url = f"sqlite:///{tmp_path / 'db.sqlite'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    yield url
    engine.dispose()


def run(capsys: pytest.CaptureFixture[str], url: str, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv), database_url=url)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli(dburl: str, capsys: pytest.CaptureFixture[str]) -> None:
    f = sessionmaker(create_engine(dburl))
    dead, live = mkjob(f, "dead_letter", "why"), mkjob(f)
    code, out, _ = run(capsys, dburl, "jobs", "list")
    assert code == 0 and len(json.loads(out)) == 2
    assert json.loads(run(capsys, dburl, "jobs", "list", "--status", "queued")[1])[0]["id"] == str(
        live
    )
    assert json.loads(run(capsys, dburl, "jobs", "show", str(live))[1])["status"] == "queued"
    assert run(capsys, dburl, "jobs", "show", str(uuid.uuid4()))[0] == 1
    assert json.loads(run(capsys, dburl, "dlq", "list")[1])[0]["id"] == str(dead)
    assert (
        json.loads(run(capsys, dburl, "dlq", "show", str(dead))[1])["dead_letter_reason"] == "why"
    )
    assert run(capsys, dburl, "dlq", "show", str(live))[0] == 1
    assert run(capsys, dburl, "dlq", "requeue", str(live))[0] == 1
    assert run(capsys, dburl, "dlq", "requeue", str(dead))[0] == 0
    assert run(capsys, dburl, "dlq", "list")[1].strip() == "[]"
    assert json.loads(run(capsys, dburl, "costs")[1])[0]["model"] == "m1"


def test_cli_usage_and_errors(
    dburl: str, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run(capsys, dburl, "bogus")[0] == 2
    assert run(capsys, dburl, "jobs", "show", "not-a-uuid")[0] == 2
    bad = "sqlite:////nonexistent-dir/x/db.sqlite"
    code, out, err = run(capsys, bad, "costs")
    assert code == 2 and "nonexistent" not in err + out
    for k in ("GITHUB_APP_ID", "DATABASE_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir("/")
    assert cli.main(["costs"]) == 2
