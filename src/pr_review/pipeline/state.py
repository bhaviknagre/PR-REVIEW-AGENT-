"""Job state machine and Postgres leases. Postgres is the source of truth; Redis only delivers ids.

Every transition is one transaction: the guarded UPDATE of the job row and the appended event
commit together. Claiming uses ``SELECT ... FOR UPDATE SKIP LOCKED`` on Postgres (SQLAlchemy omits
it on SQLite, whose single writer gives the same exclusivity in unit tests).
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from pr_review.storage.events import (
    DecisionV1,
    EventRepository,
    StageFinishedV1,
    StageStartedV1,
)
from pr_review.storage.models import Job, StageAttempt
from pr_review.storage.sanitize import sanitize_text

# SPEC FR-22 stage order. Handlers are injected by the worker; this is only the sequence.
STAGES = ("fetch", "detect", "test", "parse", "analyse", "aggregate", "post")

QUEUED, RUNNING, SUCCEEDED, FAILED = "queued", "running", "succeeded", "failed"
DEAD_LETTER, SUPERSEDED, CANCELLED = "dead_letter", "superseded", "cancelled"

TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({RUNNING, SUPERSEDED, CANCELLED}),
    # running -> queued releases the job for another attempt (retry policy is T-14).
    RUNNING: frozenset({SUCCEEDED, FAILED, DEAD_LETTER, SUPERSEDED, CANCELLED, QUEUED}),
    FAILED: frozenset({QUEUED, DEAD_LETTER}),
    SUCCEEDED: frozenset(),
    # dead_letter -> queued is the operator requeue override (api/admin.py).
    DEAD_LETTER: frozenset({QUEUED}),
    SUPERSEDED: frozenset(),
    CANCELLED: frozenset(),
}
TERMINAL = frozenset({SUCCEEDED, DEAD_LETTER, SUPERSEDED, CANCELLED})


class TransitionError(Exception):
    """Illegal transition, or the job row no longer has the expected state."""


def check_transition(src: str, dst: str) -> None:
    if dst not in TRANSITIONS.get(src, frozenset()):
        raise TransitionError(f"illegal job transition {src!r} -> {dst!r}")


def _now() -> datetime:
    return datetime.now(UTC)


def transition(session: Session, job_id: uuid.UUID, src: str, dst: str, reason: str = "") -> None:
    """Move job src -> dst atomically with its event. Raises if src is stale (lost a race)."""
    check_transition(src, dst)
    values: dict[str, object] = {"status": dst}
    if dst != RUNNING:
        values.update(leased_by=None, lease_expires_at=None)
    if dst in TERMINAL:
        values["finished_at"] = _now()
    if dst == DEAD_LETTER:
        values["dead_letter_reason"] = sanitize_text(reason)
    if src == DEAD_LETTER:  # operator requeue: clear the terminal markers
        values.update(dead_letter_reason=None, finished_at=None)
    done = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == src)
        .values(**values)
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    )
    if done.first() is None:
        session.rollback()
        raise TransitionError(f"job {job_id} is no longer {src!r}")
    EventRepository(session).append(
        job_id, DecisionV1(name="transition", reason=f"{src}->{dst}: {reason}"[:500])
    )
    session.commit()


def claim(
    session: Session, job_id: uuid.UUID, owner: str, ttl: timedelta, now: datetime | None = None
) -> bool:
    """Lease the job if it is queued or its lease expired. False means someone else has it."""
    now = now or _now()
    job = session.scalar(
        select(Job)
        .where(
            Job.id == job_id,
            or_(
                Job.status == QUEUED,
                (Job.status == RUNNING) & (Job.lease_expires_at < now),
            ),
        )
        .with_for_update(skip_locked=True)
    )
    if job is None:
        session.rollback()
        return False
    reclaimed = job.status == RUNNING
    job.status, job.leased_by, job.lease_expires_at = RUNNING, owner, now + ttl
    event = (
        DecisionV1(name="lease_reclaimed", reason=f"owner {owner}")
        if reclaimed
        else DecisionV1(name="transition", reason=f"{QUEUED}->{RUNNING}: owner {owner}")
    )
    EventRepository(session).append(job_id, event)
    session.commit()
    return True


def heartbeat(
    session: Session, job_id: uuid.UUID, owner: str, ttl: timedelta, now: datetime | None = None
) -> bool:
    """Extend our lease. False means we lost it (expired and taken, or job no longer running)."""
    now = now or _now()
    done = session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == RUNNING, Job.leased_by == owner)
        .values(lease_expires_at=now + ttl)
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    )
    ok = done.first() is not None
    session.commit()
    return ok


def completed_stages(session: Session, job_id: uuid.UUID) -> set[str]:
    return set(
        session.scalars(
            select(StageAttempt.stage).where(
                StageAttempt.job_id == job_id, StageAttempt.status == "succeeded"
            )
        )
    )


def start_stage(session: Session, job_id: uuid.UUID, stage: str) -> StageAttempt:
    last = session.scalar(
        select(func.max(StageAttempt.attempt_no)).where(
            StageAttempt.job_id == job_id, StageAttempt.stage == stage
        )
    )
    attempt = StageAttempt(
        job_id=job_id, stage=stage, attempt_no=(last or 0) + 1, status="running", started_at=_now()
    )
    session.add(attempt)
    session.flush()
    EventRepository(session).append(job_id, StageStartedV1(attempt_no=attempt.attempt_no), stage)
    session.commit()
    return attempt


def finish_stage(
    session: Session, attempt: StageAttempt, error: str | None, duration_ms: int
) -> None:
    attempt.status = "failed" if error else "succeeded"
    attempt.error = sanitize_text(error) if error else None
    attempt.finished_at = _now()
    EventRepository(session).append(
        attempt.job_id,
        StageFinishedV1(
            attempt_no=attempt.attempt_no,
            outcome=attempt.status,
            duration_ms=duration_ms,
            error=attempt.error,
        ),
        attempt.stage,
    )
    session.commit()
