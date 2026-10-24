"""Retry/backoff/dead-letter policy. SQLite in-memory; jitter and clock are injected."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from pr_review.connectors.github.auth import Forbidden, NotFound, Unauthorized, Unavailable
from pr_review.connectors.github.client import DiffTooLarge, RateLimited
from pr_review.core.metrics import DLQ_SIZE, JOBS
from pr_review.pipeline import retry, state
from pr_review.runner.workspace import GitTimeoutError, WorkspaceTooLargeError
from pr_review.storage.models import Base, Event, Job, StageAttempt
from pr_review.storage.repositories import JobRepository

NOW = datetime(2026, 1, 1, tzinfo=UTC)
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3"


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)


def _running(f: sessionmaker[Session]) -> tuple[Session, uuid.UUID]:
    s = f()
    job = JobRepository(s).create("o/r", 1, uuid.uuid4().hex, 7)
    s.commit()
    assert state.claim(s, job.id, "w1", timedelta(minutes=5))
    return s, job.id


def _fail(
    s: Session, job_id: uuid.UUID, exc: BaseException, stage: str = "fetch"
) -> retry.Decision:
    attempt = state.start_stage(s, job_id, stage)
    return retry.handle_failure(s, job_id, attempt, exc, 5, rng=lambda: 0.5, now=NOW)


def _status(s: Session, job_id: uuid.UUID) -> str:
    s.expire_all()
    return s.get_one(Job, job_id).status


class _Bad(BaseModel):
    x: int


def _validation_error() -> ValidationError:
    try:
        _Bad(x="no")  # type: ignore[arg-type]
    except ValidationError as e:
        return e
    raise AssertionError


def _http(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://x")
    return httpx.HTTPStatusError("e", request=req, response=httpx.Response(code, request=req))


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError(), True),
        (ConnectionResetError(), True),
        (httpx.ReadTimeout("t"), True),
        (httpx.ConnectError("c"), True),
        (Unavailable(), True),
        (RateLimited(), True),
        (GitTimeoutError(), True),
        (retry.LeaseLost(), True),
        (_http(503), True),
        (_http(429), True),
        (RuntimeError("unknown"), True),  # safe default: bounded retry beats losing the job
        (_validation_error(), False),
        (ValueError("bad"), False),
        (Unauthorized(), False),
        (Forbidden(), False),
        (NotFound(), False),
        (_http(404), False),
        (_http(401), False),
        (DiffTooLarge(), False),
        (WorkspaceTooLargeError(), False),
        (state.TransitionError(), False),
    ],
)
def test_classification(exc: BaseException, expected: bool) -> None:
    assert retry.is_retryable(exc) is expected


def test_backoff_bounds_and_hint() -> None:
    _, base, cap = retry.POLICIES["fetch"]
    assert retry.backoff_delay("fetch", 1, rng=lambda: 0.0) == 0.0
    for n in range(1, 12):
        d = retry.backoff_delay("fetch", n, rng=lambda: 0.999999)
        assert d <= min(cap, base * 2 ** (n - 1))
    assert retry.backoff_delay("fetch", 11, rng=lambda: 1.0) == cap
    assert retry.backoff_delay("fetch", 1, hint=30.0, rng=lambda: 0.5) == 30.0

    class Hinted(Exception):
        retry_after = "12"

    assert retry.retry_after(Hinted()) == 12.0
    assert retry.retry_after(ValueError()) is None


def test_retry_then_success(factory: sessionmaker[Session]) -> None:
    s, jid = _running(factory)
    d = _fail(s, jid, TimeoutError("slow"))
    assert d.action == "retry"
    assert d.delay == retry.backoff_delay("fetch", 1, rng=lambda: 0.5)
    assert _status(s, jid) == state.QUEUED
    row = s.scalars(select(StageAttempt)).one()
    assert (row.attempt_no, row.status, row.error) == (1, "failed", "TimeoutError: slow")
    assert row.next_retry_at is not None

    assert state.claim(s, jid, "w2", timedelta(minutes=5))
    ok = state.start_stage(s, jid, "fetch")
    assert ok.attempt_no == 2
    state.finish_stage(s, ok, None, 3)
    assert state.completed_stages(s, jid) == {"fetch"}


def test_exhaustion_dead_letters_with_reason(factory: sessionmaker[Session]) -> None:
    s, jid = _running(factory)
    max_attempts = retry.POLICIES["post"][0]
    before = DLQ_SIZE._value.get()
    dead_before = JOBS.labels("dead_letter")._value.get()
    for n in range(1, max_attempts + 1):
        d = _fail(s, jid, Unavailable("503"), "post")
        if n < max_attempts:
            assert d.action == "retry"
            assert state.claim(s, jid, "w", timedelta(minutes=5))
    assert d.action == "dead_letter"
    assert _status(s, jid) == state.DEAD_LETTER
    reason = s.get_one(Job, jid).dead_letter_reason
    assert (
        reason == f"stage post attempts exhausted after {max_attempts} attempt(s): Unavailable: 503"
    )
    assert DLQ_SIZE._value.get() == before + 1
    assert JOBS.labels("dead_letter")._value.get() == dead_before + 1
    events = s.scalars(select(Event)).all()
    assert any("dead_letter" in e.payload.get("reason", "") for e in events)


def test_permanent_error_dead_letters_immediately(factory: sessionmaker[Session]) -> None:
    s, jid = _running(factory)
    d = _fail(s, jid, Forbidden("no access"))
    assert d.action == "dead_letter"
    assert "permanent error after 1 attempt(s): Forbidden: no access" in d.reason
    assert _status(s, jid) == state.DEAD_LETTER


def test_token_is_redacted(factory: sessionmaker[Session]) -> None:
    s, jid = _running(factory)
    _fail(s, jid, Forbidden(f"bad credentials {TOKEN}"))
    stored = s.get_one(Job, jid).dead_letter_reason or ""
    err = s.scalars(select(StageAttempt)).one().error or ""
    blob = stored + err + repr(s.scalars(select(Event)).all()[-1].payload)
    assert TOKEN not in blob
    assert "[REDACTED]" in stored


def test_duplicate_invocation_not_double_counted(factory: sessionmaker[Session]) -> None:
    s, jid = _running(factory)
    attempt = state.start_stage(s, jid, "fetch")
    exc = Forbidden("x")
    first = retry.handle_failure(s, jid, attempt, exc, 1, now=NOW)
    size = DLQ_SIZE._value.get()
    events = len(s.scalars(select(Event)).all())
    assert first.action == "dead_letter"
    assert retry.handle_failure(s, jid, attempt, exc, 1, now=NOW).action == "duplicate"
    assert DLQ_SIZE._value.get() == size
    assert len(s.scalars(select(Event)).all()) == events
    assert len(s.scalars(select(StageAttempt)).all()) == 1


def test_lost_race_leaves_other_outcome(factory: sessionmaker[Session]) -> None:
    s, jid = _running(factory)
    attempt = state.start_stage(s, jid, "fetch")
    state.transition(s, jid, state.RUNNING, state.CANCELLED)
    assert retry.handle_failure(s, jid, attempt, TimeoutError(), 1, now=NOW).action == "stopped"
    assert _status(s, jid) == state.CANCELLED
