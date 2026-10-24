"""Append-only event log with versioned stage payload schemas.

Application code can only insert and read events. An ORM-level guard rejects UPDATE/DELETE of
``Event`` rows issued through a session. There is deliberately NO database trigger: the retention
purge deletes old jobs and the ``events.job_id ON DELETE CASCADE`` foreign key removes their events
at the database level, which bypasses the ORM.
"""

import uuid
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import event, select
from sqlalchemy.orm import ORMExecuteState, Session
from sqlalchemy.orm.session import SessionTransaction

from pr_review.storage.models import Event


class EventLogError(Exception):
    """Base error for the event log."""


class AppendOnlyViolation(EventLogError):
    """Raised on any attempt to update or delete an Event through a session."""


class UnknownSchemaVersion(EventLogError):
    """Raised on read when a stored (type, schema_version) has no registered payload model."""


class EventPayload(BaseModel):
    """Base for payloads: strict (no extra keys) and size-bounded, so no source or diff text."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    event_type: ClassVar[str]
    version: ClassVar[int]


_REGISTRY: dict[tuple[str, int], type[EventPayload]] = {}


def register[T: type[EventPayload]](cls: T) -> T:
    _REGISTRY[(cls.event_type, cls.version)] = cls
    return cls


@register
class StageStartedV1(EventPayload):
    event_type: ClassVar[str] = "stage_started"
    version: ClassVar[int] = 1
    attempt_no: int = Field(ge=1)


@register
class StageFinishedV1(EventPayload):
    event_type: ClassVar[str] = "stage_finished"
    version: ClassVar[int] = 1
    attempt_no: int = Field(ge=1)
    outcome: str = Field(max_length=32)
    duration_ms: int = Field(ge=0)
    error: str | None = Field(default=None, max_length=500)  # sanitized message only


@register
class DecisionV1(EventPayload):
    event_type: ClassVar[str] = "decision"
    version: ClassVar[int] = 1
    name: str = Field(max_length=64)
    reason: str = Field(max_length=500)  # sanitized prose, never file contents


def parse_payload(event_type: str, schema_version: int, data: dict[str, Any]) -> EventPayload:
    model = _REGISTRY.get((event_type, schema_version))
    if model is None:
        raise UnknownSchemaVersion(
            f"unknown schema version {schema_version} for event type {event_type!r}"
        )
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise EventLogError(f"stored {event_type!r} v{schema_version} payload is invalid") from exc


class EventRepository:
    """Insert and read only: there is intentionally no update or delete method."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(self, job_id: uuid.UUID, payload: EventPayload, stage: str | None = None) -> Event:
        row = Event(
            job_id=job_id,
            type=payload.event_type,
            stage=stage,
            schema_version=payload.version,
            payload=payload.model_dump(mode="json"),
        )
        self._session.add(row)
        self._session.flush()
        return row

    def timeline(self, job_id: uuid.UUID) -> list[tuple[Event, EventPayload]]:
        rows = self._session.scalars(
            select(Event).where(Event.job_id == job_id).order_by(Event.id)
        ).all()
        return [(r, parse_payload(r.type, r.schema_version, r.payload)) for r in rows]


@event.listens_for(Session, "before_flush")
def _reject_event_mutation(session: Session, _ctx: SessionTransaction | None, _inst: Any) -> None:
    for obj in session.deleted:
        if isinstance(obj, Event):
            raise AppendOnlyViolation("events are append-only: delete rejected")
    for obj in session.dirty:
        if isinstance(obj, Event) and session.is_modified(obj):
            raise AppendOnlyViolation("events are append-only: update rejected")


@event.listens_for(Session, "do_orm_execute")
def _reject_bulk_event_mutation(state: ORMExecuteState) -> None:
    if (state.is_update or state.is_delete) and any(m.class_ is Event for m in state.all_mappers):
        raise AppendOnlyViolation("events are append-only: bulk update/delete rejected")
