"""State machine, lease and worker tests. SQLite in-memory: it has one writer and ignores
FOR UPDATE SKIP LOCKED, so claim exclusivity here proves the guarded-status logic, not row-lock
behaviour; the Postgres lock path needs a real-Postgres integration test (follow-up)."""

import asyncio
import functools
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from arq.connections import ArqRedis
from fakeredis import FakeAsyncRedis
from sqlalchemy import create_engine, event, select, update
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from pr_review.core.settings import WorkerConfig
from pr_review.pipeline import lease, queue, retry, state
from pr_review.storage.models import Base, Event, Job, StageAttempt
from pr_review.storage.repositories import JobRepository

TTL = timedelta(minutes=5)


def arun(fn):  # type: ignore[no-untyped-def]
    """Run an async test with asyncio.run (no async pytest plugin is installed)."""

    @functools.wraps(fn)
    def wrapper(*a, **k):  # type: ignore[no-untyped-def]
        return asyncio.run(fn(*a, **k))

    return wrapper


@pytest.fixture
def factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)


def _job(f: sessionmaker[Session]) -> uuid.UUID:
    with f() as s:
        job = JobRepository(s).create("o/r", 1, uuid.uuid4().hex, 7)
        s.commit()
        return job.id


def _status(f: sessionmaker[Session], jid: uuid.UUID) -> str:
    with f() as s:
        return s.scalars(select(Job.status).where(Job.id == jid)).one()


def _events(f: sessionmaker[Session], jid: uuid.UUID) -> list[Event]:
    with f() as s:
        return list(s.scalars(select(Event).where(Event.job_id == jid).order_by(Event.id)))


@pytest.mark.parametrize(
    ("src", "dst"),
    [(s, d) for s, ds in state.TRANSITIONS.items() for d in ds],
)
def test_legal_transitions(src: str, dst: str) -> None:
    state.check_transition(src, dst)


@pytest.mark.parametrize(
    ("src", "dst"),
    [("queued", "succeeded"), ("succeeded", "queued"), ("cancelled", "running"), ("x", "queued")],
)
def test_illegal_transitions(src: str, dst: str) -> None:
    with pytest.raises(state.TransitionError):
        state.check_transition(src, dst)


def test_transition_appends_event_and_rejects_stale(factory: sessionmaker[Session]) -> None:
    jid = _job(factory)
    with factory() as s:
        state.transition(s, jid, "queued", "cancelled", "user")
        assert _status(factory, jid) == "cancelled"
        assert len(_events(factory, jid)) == 1
        with pytest.raises(state.TransitionError):
            state.transition(s, jid, "queued", "running")
    assert len(_events(factory, jid)) == 1


def test_claim_exclusive_and_event(factory: sessionmaker[Session]) -> None:
    jid = _job(factory)
    with factory() as a, factory() as b:
        assert state.claim(a, jid, "a", TTL)
        assert not state.claim(b, jid, "b", TTL)
    assert _status(factory, jid) == "running"
    assert len(_events(factory, jid)) == 1


def test_expired_lease_is_claimable_and_heartbeat_extends(
    factory: sessionmaker[Session],
) -> None:
    jid = _job(factory)
    t0 = datetime.now(UTC)
    with factory() as s:
        assert state.claim(s, jid, "a", TTL, now=t0)
        later = t0 + TTL + timedelta(seconds=1)
        assert state.heartbeat(s, jid, "a", TTL, now=t0 + timedelta(minutes=4))
        assert not state.claim(s, jid, "b", TTL, now=later)  # extended, still held
        assert state.claim(s, jid, "b", TTL, now=later + timedelta(minutes=5))
        assert not state.heartbeat(s, jid, "a", TTL)  # a lost it


def _stages(calls: list[str], cancel_after: str | None = None, f: object = None):  # type: ignore[no-untyped-def]
    handlers: dict[str, queue.Stage] = {}
    for name in state.STAGES:

        async def h(jid: uuid.UUID, name: str = name) -> None:
            calls.append(name)
            if name == cancel_after:
                with f() as s:  # type: ignore[operator]
                    s.execute(update(Job).where(Job.id == jid).values(status="cancelled"))
                    s.commit()

        handlers[name] = h
    return handlers


@arun
async def test_full_run_records_attempts(factory: sessionmaker[Session]) -> None:
    jid = _job(factory)
    calls: list[str] = []
    assert await queue.run_job(factory, _stages(calls), jid, "w") == "succeeded"
    assert calls == list(state.STAGES)
    with factory() as s:
        assert state.completed_stages(s, jid) == set(state.STAGES)
        assert s.scalars(select(Job.leased_by)).one() is None


@arun
async def test_cancellation_stops_before_next_stage(factory: sessionmaker[Session]) -> None:
    jid = _job(factory)
    calls: list[str] = []
    out = await queue.run_job(factory, _stages(calls, "detect", factory), jid, "w")
    assert out == "stopped"
    assert calls == ["fetch", "detect"]
    assert _status(factory, jid) == "cancelled"


@arun
async def test_duplicate_delivery_does_not_double_run(factory: sessionmaker[Session]) -> None:
    jid = _job(factory)
    calls: list[str] = []
    assert await queue.run_job(factory, _stages(calls), jid, "w1") == "succeeded"
    assert await queue.run_job(factory, _stages(calls), jid, "w2") == "skipped"
    assert calls == list(state.STAGES)


async def _boom(_: uuid.UUID) -> None:
    raise RuntimeError("bad")


@arun
async def test_stage_failure_uses_handle_failure_and_defers(
    factory: sessionmaker[Session],
) -> None:
    jid = _job(factory)
    stages = _stages([])
    stages["detect"] = _boom
    sent: list[tuple[uuid.UUID, float]] = []

    async def enqueue(j: uuid.UUID, delay: float) -> None:
        sent.append((j, delay))

    assert await queue.run_job(factory, stages, jid, "w", enqueue=enqueue) == "queued"
    assert _status(factory, jid) == "queued"
    assert [j for j, _ in sent] == [jid]
    assert 0 <= sent[0][1] <= retry.POLICIES["detect"][2]
    with factory() as s:
        assert [a.status for a in s.scalars(select(StageAttempt).order_by(StageAttempt.id))] == [
            "succeeded",
            "failed",
        ]
    calls: list[str] = []
    assert await queue.run_job(factory, _stages(calls), jid, "w") == "succeeded"
    assert calls == list(state.STAGES[1:])  # resumed after the last completed stage


@arun
async def test_permanent_failure_dead_letters(factory: sessionmaker[Session]) -> None:
    jid = _job(factory)
    stages = _stages([])

    async def bad(_: uuid.UUID) -> None:
        raise ValueError("nope")

    stages["fetch"] = bad
    assert await queue.run_job(factory, stages, jid, "w") == "dead_letter"
    assert _status(factory, jid) == "dead_letter"


@arun
async def test_stage_timeout_is_retryable(factory: sessionmaker[Session]) -> None:
    jid = _job(factory)
    stages = _stages([])

    async def slow(_: uuid.UUID) -> None:
        await asyncio.sleep(5)

    stages["fetch"] = slow
    monkey = lease.STAGE_TIMEOUTS.copy()
    lease.STAGE_TIMEOUTS["fetch"] = 0.05
    try:
        assert await queue.run_job(factory, stages, jid, "w") == "queued"
    finally:
        lease.STAGE_TIMEOUTS.clear()
        lease.STAGE_TIMEOUTS.update(monkey)
    with factory() as s:
        err = s.scalars(select(StageAttempt.error)).one()
    assert err is not None and err.startswith("TimeoutError")


@arun
async def test_deadline_dead_letters(factory: sessionmaker[Session]) -> None:
    jid = _job(factory)
    calls: list[str] = []
    late = lambda: datetime.now(UTC) + timedelta(hours=2)  # noqa: E731
    out = await queue.run_job(factory, _stages(calls), jid, "w", clock=late)
    assert out == "dead_letter" and calls == []
    with factory() as s:
        assert s.scalars(select(Job.dead_letter_reason)).one() == "deadline exceeded"


def _expire(f: sessionmaker[Session], jid: uuid.UUID, owner: str = "dead") -> datetime:
    past = datetime.now(UTC) - timedelta(minutes=10)
    with f() as s:
        s.execute(
            update(Job)
            .where(Job.id == jid)
            .values(status="running", leased_by=owner, lease_expires_at=past)
        )
        s.commit()
    return datetime.now(UTC)


@arun
async def test_expired_lease_mid_stage_is_retried_and_resumes(
    factory: sessionmaker[Session],
) -> None:
    jid = _job(factory)
    calls: list[str] = []
    await queue.run_job(factory, _stages(calls), jid, "w")  # completes everything
    with factory() as s:  # simulate a crash during "post": drop its success, leave it running
        s.execute(update(StageAttempt).where(StageAttempt.stage == "post").values(status="running"))
        s.commit()
    now = _expire(factory, jid)
    sent: list[tuple[uuid.UUID, float]] = []

    async def enqueue(j: uuid.UUID, delay: float) -> None:
        sent.append((j, delay))

    cfg = WorkerConfig()
    assert await lease.recover_expired(factory, enqueue, cfg, now=now) == 1
    assert _status(factory, jid) == "queued" and [j for j, _ in sent] == [jid]
    again = await lease.recover_expired(factory, enqueue, cfg, now=now)
    assert again == 0  # no longer running
    calls.clear()
    assert await queue.run_job(factory, _stages(calls), jid, "w2") == "succeeded"
    assert calls == ["post"]  # only the interrupted stage re-ran
    msgs = " ".join(str(e.payload) for e in _events(factory, jid))
    assert "LeaseLost" in msgs


@arun
async def test_expired_lease_between_stages_requeues(factory: sessionmaker[Session]) -> None:
    jid = _job(factory)
    now = _expire(factory, jid)
    sent: list[float] = []

    async def enqueue(_: uuid.UUID, delay: float) -> None:
        sent.append(delay)

    assert await lease.recover_expired(factory, enqueue, WorkerConfig(), now=now) == 1
    assert _status(factory, jid) == "queued" and sent == [0.0]


@arun
async def test_expired_lease_past_deadline_dead_letters(factory: sessionmaker[Session]) -> None:
    jid = _job(factory)
    now = _expire(factory, jid) + timedelta(hours=2)
    sent: list[float] = []

    async def enqueue(_: uuid.UUID, delay: float) -> None:
        sent.append(delay)

    assert await lease.recover_expired(factory, enqueue, WorkerConfig(), now=now) == 1
    assert _status(factory, jid) == "dead_letter" and sent == []


@arun
async def test_expired_lease_exhausted_attempts_dead_letters(
    factory: sessionmaker[Session],
) -> None:
    jid = _job(factory)
    with factory() as s:
        state.claim(s, jid, "w", TTL)
        for _ in range(retry.POLICIES["fetch"][0]):
            state.start_stage(s, jid, "fetch")
            s.execute(
                update(StageAttempt).where(StageAttempt.status == "failed").values(status="x")
            )
    now = _expire(factory, jid)

    async def enqueue(_: uuid.UUID, __: float) -> None:
        raise AssertionError("must not enqueue")

    await lease.recover_expired(factory, enqueue, WorkerConfig(), now=now)
    assert _status(factory, jid) == "dead_letter"


@arun
async def test_enqueuer_is_idempotent() -> None:
    pool = ArqRedis(connection_pool=FakeAsyncRedis().connection_pool)
    enq = queue.ArqEnqueuer(pool)
    jid = uuid.uuid4()
    await enq.enqueue(jid)
    await enq.enqueue(jid)
    assert len(await pool.queued_jobs()) == 1


@arun
async def test_sweeper_requeues_stale_queued_only(factory: sessionmaker[Session]) -> None:
    stale, fresh, leased = _job(factory), _job(factory), _job(factory)
    old = datetime.now(UTC) - timedelta(hours=1)
    with factory() as s:
        s.execute(update(Job).where(Job.id.in_([stale, leased])).values(updated_at=old))
        s.execute(update(Job).where(Job.id == leased).values(leased_by="w"))
        s.commit()
    pool = ArqRedis(connection_pool=FakeAsyncRedis().connection_pool)
    ctx = {"session_factory": factory, "redis": pool}
    assert await queue.sweep_stale_queued(ctx) == 1
    assert [j.args[0] for j in await pool.queued_jobs()] == [str(stale)]
    assert fresh  # untouched


def test_worker_settings_concurrency() -> None:
    assert queue.WorkerSettings.max_jobs == queue.CFG.worker_max_jobs


@arun
async def test_dead_letter_reports_failed_stage_once(factory: sessionmaker[Session]) -> None:
    jid = _job(factory)
    stages = _stages([])
    reports: list[tuple[uuid.UUID, str, str]] = []

    async def bad(_: uuid.UUID) -> None:
        raise ValueError("nope")

    async def on_failure(j: uuid.UUID, stage: str, reason: str) -> None:
        reports.append((j, stage, reason))
        assert _status(factory, j) == "dead_letter"  # hook runs after the transition

    stages["test"] = bad
    out = await queue.run_job(factory, stages, jid, "w", on_failure=on_failure)
    assert out == "dead_letter"
    assert len(reports) == 1 and reports[0][:2] == (jid, "test") and "nope" in reports[0][2]


@arun
async def test_failure_hook_skipped_on_retry_and_errors_do_not_change_outcome(
    factory: sessionmaker[Session],
) -> None:
    called: list[str] = []

    async def on_failure(_: uuid.UUID, stage: str, __: str) -> None:
        called.append(stage)
        raise RuntimeError("post failed")

    retried = _stages([])
    retried["detect"] = _boom  # retryable: no hook
    assert await queue.run_job(factory, retried, _job(factory), "w", on_failure=on_failure) == (
        "queued"
    )
    assert called == []

    async def bad(_: uuid.UUID) -> None:
        raise ValueError("nope")

    dead = _stages([])
    dead["parse"] = bad
    jid = _job(factory)
    assert await queue.run_job(factory, dead, jid, "w", on_failure=on_failure) == "dead_letter"
    assert called == ["parse"] and _status(factory, jid) == "dead_letter"


@arun
async def test_cancelled_job_does_not_report_failure(factory: sessionmaker[Session]) -> None:
    jid = _job(factory)
    called: list[str] = []

    async def on_failure(_: uuid.UUID, stage: str, __: str) -> None:
        called.append(stage)

    out = await queue.run_job(
        factory, _stages([], "fetch", factory), jid, "w", on_failure=on_failure
    )
    assert out == "stopped" and called == []


@arun
async def test_worker_startup_registers_stages_provider_and_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for k, v in {
        "GITHUB_APP_ID": "1",
        "GITHUB_PRIVATE_KEY": "pk",
        "GITHUB_WEBHOOK_SECRET": "whsec",
        "DATABASE_URL": "sqlite://",
        "REDIS_URL": "redis://localhost:6379/0",
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir("/")
    from pr_review.core.settings import get_settings

    get_settings.cache_clear()
    saved = dict(queue.STAGE_HANDLERS)
    ctx: dict[str, object] = {}
    try:
        await queue.on_startup(ctx)
        assert set(queue.STAGE_HANDLERS) == set(state.STAGES)
        assert ctx["provider"] is not None and ctx["on_failure"] is not None
        names = {c.name for c in queue.WorkerSettings.cron_jobs}
        assert any("retention_job" in n for n in names)
        assert queue.WorkerSettings.on_shutdown is queue.on_shutdown
        await queue.on_shutdown(ctx)
    finally:
        queue.STAGE_HANDLERS.clear()
        queue.STAGE_HANDLERS.update(saved)
        get_settings.cache_clear()
