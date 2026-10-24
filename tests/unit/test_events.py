from collections.abc import Iterator

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, delete, event, text, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from pr_review.storage.events import (
    AppendOnlyViolation,
    DecisionV1,
    EventRepository,
    StageFinishedV1,
    StageStartedV1,
    UnknownSchemaVersion,
)
from pr_review.storage.models import Base, Event, Job
from pr_review.storage.repositories import DeliveryKeyRepository, JobRepository


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _job(s: Session) -> Job:
    return JobRepository(s).create("o/r", 1, "abc", 7)


def test_append_and_timeline(session: Session) -> None:
    job = _job(session)
    repo = EventRepository(session)
    repo.append(job.id, StageStartedV1(attempt_no=1), stage="fetch")
    repo.append(job.id, StageFinishedV1(attempt_no=1, outcome="ok", duration_ms=5), stage="fetch")
    got = [p for _, p in repo.timeline(job.id)]
    assert got == [
        StageStartedV1(attempt_no=1),
        StageFinishedV1(attempt_no=1, outcome="ok", duration_ms=5),
    ]


def test_no_update_delete_methods() -> None:
    assert not [n for n in dir(EventRepository) if n.startswith(("update", "delete", "remove"))]


def test_orm_update_and_delete_rejected(session: Session) -> None:
    job = _job(session)
    row = EventRepository(session).append(job.id, DecisionV1(name="n", reason="r"))
    session.commit()
    row.type = "tampered"
    with pytest.raises(AppendOnlyViolation):
        session.flush()
    session.rollback()
    row = session.query(Event).one()
    session.delete(row)
    with pytest.raises(AppendOnlyViolation):
        session.flush()
    session.rollback()
    with pytest.raises(AppendOnlyViolation):
        session.execute(update(Event).values(type="x"))
    with pytest.raises(AppendOnlyViolation):
        session.execute(delete(Event))


def test_purge_cascade_still_removes_events(session: Session) -> None:
    job = _job(session)
    EventRepository(session).append(job.id, DecisionV1(name="n", reason="r"))
    session.commit()
    session.execute(delete(Job).where(Job.id == job.id))  # what the retention purge does
    assert session.scalar(text("select count(*) from events")) == 0


def test_unknown_version_rejected_on_read(session: Session) -> None:
    job = _job(session)
    session.add(Event(job_id=job.id, type="decision", schema_version=99, payload={}))
    session.flush()
    with pytest.raises(UnknownSchemaVersion, match="99"):
        EventRepository(session).timeline(job.id)


def test_payload_rejects_extra_fields_and_oversize() -> None:
    with pytest.raises(ValidationError):
        DecisionV1.model_validate({"name": "n", "reason": "r", "diff": "+secret"})
    with pytest.raises(ValidationError):
        DecisionV1(name="n", reason="x" * 501)


def test_delivery_key_idempotent(session: Session) -> None:
    keys = DeliveryKeyRepository(session)
    assert keys.record("d1") is True
    assert keys.record("d1") is False
    assert keys.record("d2") is True
