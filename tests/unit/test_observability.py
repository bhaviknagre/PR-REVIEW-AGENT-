import uuid
from collections.abc import Iterator

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from pr_review.api.health import db_checker, redis_checker, router
from pr_review.core.metrics import observe_stage, record_job_finished, record_llm_call
from pr_review.storage.events import DecisionV1, EventRepository, StageFinishedV1, StageStartedV1
from pr_review.storage.models import Base
from pr_review.storage.repositories import JobRepository

SECRET = "postgresql://user:hunter2secret@db.internal/prod"


async def _boom() -> None:
    raise RuntimeError(SECRET)


@pytest.fixture
def factory() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def app(factory: sessionmaker[Session]) -> FastAPI:
    a = FastAPI()
    a.include_router(router)
    a.state.session_factory = factory
    a.state.db_check = db_checker(factory)
    a.state.redis_check = redis_checker(fakeredis.FakeAsyncRedis())
    return a


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def test_healthz_no_dependencies() -> None:
    a = FastAPI()
    a.include_router(router)  # nothing configured at all
    assert TestClient(a).get("/healthz").json() == {"status": "ok"}


def test_readyz_ok(client: TestClient) -> None:
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["checks"] == {"db": "ok", "redis": "ok"}


def test_readyz_failure_leaks_nothing(app: FastAPI, client: TestClient) -> None:
    app.state.redis_check = _boom
    r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"] == {"db": "ok", "redis": "fail"}
    assert "hunter2" not in r.text and "postgresql" not in r.text


def test_readyz_unconfigured_is_503() -> None:
    a = FastAPI()
    a.include_router(router)
    assert TestClient(a).get("/readyz").status_code == 503


def test_metrics_exposition(client: TestClient, factory: sessionmaker[Session]) -> None:
    with factory.begin() as s:
        JobRepository(s).create("o/r", 1, "abc", 7)
    observe_stage("fetch", 0.2)
    record_llm_call("m", 10, 5, 0.01)
    record_job_finished("succeeded")
    r = client.get("/metrics")
    assert r.status_code == 200
    for name in (
        "pr_review_queue_depth 1.0",
        "pr_review_dead_letter_jobs 0.0",
        'pr_review_stage_latency_seconds_count{stage="fetch"}',
        'pr_review_llm_cost_usd_total{model="m"}',
        'pr_review_llm_tokens_total{kind="prompt",model="m"}',
        'pr_review_jobs_total{status="succeeded"}',
    ):
        assert name in r.text


def test_timeline_ordered_and_404(client: TestClient, factory: sessionmaker[Session]) -> None:
    with factory.begin() as s:
        job = JobRepository(s).create("o/r", 1, "abc", 7)
        ev = EventRepository(s)
        ev.append(job.id, StageStartedV1(attempt_no=1), stage="fetch")
        ev.append(job.id, StageFinishedV1(attempt_no=1, outcome="ok", duration_ms=5), stage="fetch")
        ev.append(job.id, DecisionV1(name="gate", reason="small diff"))
        job_id = job.id
    r = client.get(f"/jobs/{job_id}/timeline")
    assert r.status_code == 200
    events = r.json()["events"]
    assert [e["type"] for e in events] == ["stage_started", "stage_finished", "decision"]
    assert events[0]["stage"] == "fetch" and events[0]["schema_version"] == 1
    assert events[1]["payload"]["duration_ms"] == 5 and events[0]["timestamp"]
    assert client.get(f"/jobs/{uuid.uuid4()}/timeline").status_code == 404
