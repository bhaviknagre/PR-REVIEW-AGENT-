"""Repositories. They accept metadata only: no method takes source or diff content."""

import uuid

from sqlalchemy import select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from pr_review.storage.events import EventRepository
from pr_review.storage.models import DeliveryKey, Job

__all__ = ["DeliveryKeyRepository", "EventRepository", "JobRepository"]


class JobRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, repo: str, pr_number: int, head_sha: str, installation_id: int) -> Job:
        job = Job(
            repo=repo, pr_number=pr_number, head_sha=head_sha, installation_id=installation_id
        )
        self._session.add(job)
        self._session.flush()
        return job

    def get(self, job_id: uuid.UUID) -> Job | None:
        return self._session.get(Job, job_id)

    def find(self, repo: str, pr_number: int, head_sha: str) -> Job | None:
        return self._session.scalar(
            select(Job).where(
                Job.repo == repo, Job.pr_number == pr_number, Job.head_sha == head_sha
            )
        )


class DeliveryKeyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def record(self, delivery_id: str, job_id: uuid.UUID | None = None) -> bool:
        """Insert a delivery key. True if new, False if already seen (never raises on replay)."""
        name = self._session.get_bind().dialect.name
        insert = postgresql.insert if name == "postgresql" else sqlite.insert
        stmt = (
            insert(DeliveryKey)
            .values(delivery_id=delivery_id, job_id=job_id)
            .on_conflict_do_nothing()
            .returning(DeliveryKey.delivery_id)  # rowcount is -1 on Postgres; a row means new
        )
        return self._session.execute(stmt).first() is not None
