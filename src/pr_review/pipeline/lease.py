"""Crash recovery, per-stage timeouts and the overall job deadline (FR-22, NFR-8, NFR-9).

Resume rule: a job resumes at the first stage with no succeeded StageAttempt (the worker skips
``state.completed_stages``). Completed stages are never re-run. A stage that was in flight when the
worker died may run again, so every stage handler MUST be idempotent (safe to repeat with the
same job id, e.g. upsert findings by key, post comments keyed by a stored marker).

Recovery: the sweeper re-claims running jobs whose lease expired (``state.claim`` does the
exclusive lease), counts the lost lease as a retryable ``LeaseLost`` failure of the in-flight
attempt via ``retry.handle_failure`` (retry with backoff, or dead-letter when attempts are spent),
then re-enqueues with the returned delay.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from pr_review.core.metrics import DLQ_SIZE, record_job_finished
from pr_review.core.settings import WorkerConfig
from pr_review.pipeline import retry, state
from pr_review.storage.models import Job, StageAttempt

# Seconds per stage (NFR-9); analyse uses WorkerConfig.analyse_timeout_seconds, unlisted stages
# use WorkerConfig.stage_timeout_seconds.
STAGE_TIMEOUTS: dict[str, float] = {
    "fetch": 120.0,
    "detect": 60.0,
    "test": 900.0,  # sandboxed test runs
    "parse": 60.0,
    "aggregate": 60.0,
    "post": 120.0,
}
RECOVER_BATCH = 100
DEADLINE_REASON = "deadline exceeded"

Enqueue = Callable[[uuid.UUID, float], Awaitable[None]]


def utcnow() -> datetime:
    return datetime.now(UTC)


def stage_timeout(stage: str, cfg: WorkerConfig, remaining: float) -> float:
    """Per-stage limit, never beyond what is left of the job deadline."""
    if stage == "analyse":
        limit = cfg.analyse_timeout_seconds
    else:
        limit = STAGE_TIMEOUTS.get(stage, cfg.stage_timeout_seconds)
    return max(0.0, min(limit, remaining))


def deadline_remaining(
    session: Session, job_id: uuid.UUID, cfg: WorkerConfig, now: datetime
) -> float:
    created = session.scalars(select(Job.created_at).where(Job.id == job_id)).one()
    if created.tzinfo is None:  # SQLite drops tzinfo
        created = created.replace(tzinfo=UTC)
    return (created + timedelta(seconds=cfg.job_deadline_seconds) - now).total_seconds()


def dead_letter_deadline(session: Session, job_id: uuid.UUID) -> str:
    try:
        state.transition(session, job_id, state.RUNNING, state.DEAD_LETTER, DEADLINE_REASON)
    except state.TransitionError:  # superseded/cancelled meanwhile: that outcome stands
        return "stopped"
    DLQ_SIZE.inc()
    record_job_finished(state.DEAD_LETTER)
    return state.DEAD_LETTER


def _release(session: Session, job_id: uuid.UUID, cfg: WorkerConfig, now: datetime) -> float | None:
    """Release a job we just re-claimed. Returns the enqueue delay, or None if not re-enqueued."""
    if deadline_remaining(session, job_id, cfg, now) <= 0:
        dead_letter_deadline(session, job_id)
        return None
    attempt = session.scalars(
        select(StageAttempt)
        .where(StageAttempt.job_id == job_id, StageAttempt.status == "running")
        .order_by(StageAttempt.id.desc())
    ).first()
    try:
        if attempt is None:  # died between stages: nothing in flight, just run the next one
            state.transition(session, job_id, state.RUNNING, state.QUEUED, "lease expired")
            return 0.0
    except state.TransitionError:
        return None
    decision = retry.handle_failure(
        session, job_id, attempt, retry.LeaseLost(f"lease expired during {attempt.stage}"), 0,
        now=now,
    )  # fmt: skip
    return decision.delay if decision.action == "retry" else None


async def recover_expired(
    factory: sessionmaker[Session],
    enqueue: Enqueue,
    cfg: WorkerConfig,
    owner: str = "sweeper",
    now: datetime | None = None,
) -> int:
    """Re-lease and release running jobs whose lease expired. Returns how many were recovered."""
    now = now or utcnow()
    with factory() as session:
        ids = list(
            session.scalars(
                select(Job.id)
                .where(Job.status == state.RUNNING, Job.lease_expires_at < now)
                .limit(RECOVER_BATCH)
            )
        )
    recovered = 0
    for job_id in ids:
        with factory() as session:
            if not state.claim(
                session, job_id, owner, timedelta(seconds=cfg.worker_lease_seconds), now
            ):
                continue  # another sweeper or worker got it first
            delay = _release(session, job_id, cfg, now)
        recovered += 1
        if delay is not None:
            await enqueue(job_id, delay)
    return recovered
