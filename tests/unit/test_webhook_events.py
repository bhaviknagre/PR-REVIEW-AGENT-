import hashlib
import hmac
import json
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from pr_review.api.app import create_app
from pr_review.core.settings import Settings
from pr_review.storage.models import Base, DeliveryKey, Job

SECRET = "whsec-test-secret"


class RecordingEnqueuer:
    def __init__(self) -> None:
        self.jobs: list[uuid.UUID] = []

    async def enqueue(self, job_id: uuid.UUID) -> None:
        self.jobs.append(job_id)


class Harness:
    def __init__(self) -> None:
        engine = create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
        Base.metadata.create_all(engine)
        self.factory = sessionmaker(engine)
        self.enqueuer = RecordingEnqueuer()
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            github_app_id=1,
            github_private_key="pk",  # type: ignore[arg-type]
            github_webhook_secret=SECRET,  # type: ignore[arg-type]
            database_url="postgresql://u:p@h/db",  # type: ignore[arg-type]
            redis_url="redis://h/0",  # type: ignore[arg-type]
        )
        app = create_app(settings)
        app.state.session_factory = self.factory
        app.state.enqueuer = self.enqueuer
        self.app = app
        self.client = TestClient(app)

    def send(
        self,
        action: str = "opened",
        event_name: str = "pull_request",
        delivery: str | None = "d-1",
        sha: str = "abc",
    ) -> tuple[int, str]:
        body = json.dumps(
            {
                "action": action,
                "repository": {"full_name": "o/r"},
                "pull_request": {"number": 5, "head": {"sha": sha}},
                "installation": {"id": 9},
            }
        ).encode()
        sig = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers = {"X-Hub-Signature-256": sig, "X-GitHub-Event": event_name}
        if delivery:
            headers["X-GitHub-Delivery"] = delivery
        r = self.client.post("/webhook/github", content=body, headers=headers)
        return r.status_code, r.text

    def count(self, model: type[Job] | type[DeliveryKey]) -> int:
        with Session(self.factory.kw["bind"]) as s:
            return s.scalar(select(func.count()).select_from(model)) or 0


@pytest.fixture
def h() -> Iterator[Harness]:
    yield Harness()


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened", "ready_for_review"])
def test_accepted_actions_persist_then_enqueue(h: Harness, action: str) -> None:
    assert h.send(action) == (202, "accepted")
    assert h.count(Job) == 1 and h.count(DeliveryKey) == 1
    assert len(h.enqueuer.jobs) == 1


@pytest.mark.parametrize(
    "action,event_name",
    [("closed", "pull_request"), ("labeled", "pull_request"), ("opened", "issues")],
)
def test_ignored_without_persisting(h: Harness, action: str, event_name: str) -> None:
    assert h.send(action, event_name) == (202, "ignored")
    assert h.count(Job) == 0 and h.count(DeliveryKey) == 0 and not h.enqueuer.jobs


def test_ping(h: Harness) -> None:
    assert h.send(event_name="ping")[0] == 200


def test_duplicate_delivery(h: Harness) -> None:
    h.send()
    assert h.send() == (202, "duplicate")
    assert h.count(Job) == 1 and len(h.enqueuer.jobs) == 1


def test_same_sha_new_delivery_not_requeued(h: Harness) -> None:
    h.send(delivery="d-1")
    assert h.send(delivery="d-2") == (202, "duplicate")
    assert h.count(Job) == 1 and h.count(DeliveryKey) == 2 and len(h.enqueuer.jobs) == 1


def test_new_sha_makes_new_job(h: Harness) -> None:
    h.send(delivery="d-1")
    h.send("synchronize", delivery="d-2", sha="def")
    assert h.count(Job) == 2 and len(h.enqueuer.jobs) == 2


def test_persistence_failure_is_503_and_never_enqueues(h: Harness) -> None:
    with Session(h.factory.kw["bind"]) as s:
        s.connection().exec_driver_sql("DROP TABLE delivery_keys")
        s.commit()
    assert h.send()[0] == 503
    assert not h.enqueuer.jobs


def test_enqueue_failure_keeps_job_and_acks(h: Harness) -> None:
    async def boom(job_id: uuid.UUID) -> None:
        raise RuntimeError("redis down")

    h.enqueuer.enqueue = boom  # type: ignore[method-assign]
    assert h.send()[0] == 202
    assert h.count(Job) == 1


def test_missing_delivery_header_is_400(h: Harness) -> None:
    assert h.send(delivery=None)[0] == 400


def test_unconfigured_app_is_503(h: Harness) -> None:
    del h.app.state.enqueuer
    assert h.send()[0] == 503
