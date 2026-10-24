"""Supersede: a newly accepted head SHA cancels queued/running jobs for the same PR.

Rule: within one (repo, PR) the job accepted LAST wins. "Last" is decided by a per-PR
transaction-scoped advisory lock on Postgres (SQLite serialises writers itself): whoever takes
the lock second sees the other's committed job and supersedes it, never the reverse, so two
concurrent webhooks cannot cancel each other's newest job.
ponytail: acceptance order, not GitHub event time; a late-delivered older SHA would win.
"""

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from pr_review.storage.events import DecisionV1, EventRepository
from pr_review.storage.models import Job

ACTIVE = ("queued", "running")
CANCELLED = ("superseded", "cancelled")


def _lock_key(repo: str, pr_number: int) -> int:
    digest = hashlib.blake2b(f"{repo}#{pr_number}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def supersede_older(session: Session, job: Job) -> list[uuid.UUID]:
    """Mark other queued/running jobs of job's PR superseded, with an event each. Call in the
    same transaction that created ``job``. Returns the superseded job ids."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key(job.repo, job.pr_number)}
        )
    ids = list(
        session.scalars(
            update(Job)
            .where(
                Job.repo == job.repo,
                Job.pr_number == job.pr_number,
                Job.id != job.id,
                Job.status.in_(ACTIVE),
            )
            .values(status="superseded", finished_at=datetime.now(UTC))
            .returning(Job.id)
            .execution_options(synchronize_session=False)
        )
    )
    events = EventRepository(session)
    reason = f"superseded by head sha {job.head_sha[:12]}"[:500]
    for old in ids:
        events.append(old, DecisionV1(name="superseded", reason=reason))
    return ids


def is_cancelled(session: Session, job_id: uuid.UUID) -> bool:
    """Cooperative check for the pipeline between stages. Reads the DB, not the identity map."""
    status = session.scalar(select(Job.status).where(Job.id == job_id))
    return status in CANCELLED
