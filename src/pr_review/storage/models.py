"""SQLAlchemy 2 models. Postgres is the source of truth.

Privacy: no column stores source code or raw diff content, only sanitized findings and metadata.
Every table has an indexed ``created_at`` so the retention purge can range-scan it.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# JSONB on Postgres, plain JSON elsewhere (SQLite in unit tests).
Json = JSON().with_variant(JSONB(), "postgresql")
Timestamp = DateTime(timezone=True)

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class _Created:
    created_at: Mapped[datetime] = mapped_column(
        Timestamp, server_default=func.now(), nullable=False
    )


def _job_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)


class Job(_Created, Base):
    """One review of (repo, PR, head SHA). Status/lease columns drive retries and dead-letter."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("repo", "pr_number", "head_sha"),
        Index("ix_jobs_created_at", "created_at"),
        Index("ix_jobs_status_lease_expires_at", "status", "lease_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    repo: Mapped[str] = mapped_column(String(255))
    pr_number: Mapped[int] = mapped_column(Integer)
    head_sha: Mapped[str] = mapped_column(String(64))
    installation_id: Mapped[int] = mapped_column(BigInteger)
    # queued | running | succeeded | failed | dead_letter | superseded | cancelled
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    dead_letter_reason: Mapped[str | None] = mapped_column(Text)
    leased_by: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(Timestamp)
    updated_at: Mapped[datetime] = mapped_column(
        Timestamp, server_default=func.now(), onupdate=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(Timestamp)


class DeliveryKey(_Created, Base):
    """Idempotency: X-GitHub-Delivery IDs are unique; a replay hits the primary key."""

    __tablename__ = "delivery_keys"
    __table_args__ = (Index("ix_delivery_keys_created_at", "created_at"),)

    delivery_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # NULL for deliveries that were acknowledged but filtered out (no job created).
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )


class StageAttempt(_Created, Base):
    """One try of one pipeline stage; retries add rows, so history is never overwritten."""

    __tablename__ = "stage_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "stage", "attempt_no"),
        Index("ix_stage_attempts_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    job_id: Mapped[uuid.UUID] = _job_fk()
    stage: Mapped[str] = mapped_column(String(64))
    attempt_no: Mapped[int] = mapped_column(Integer)
    # running | succeeded | failed | dead_letter
    status: Mapped[str] = mapped_column(String(32))
    error: Mapped[str | None] = mapped_column(Text)  # sanitized message, never source
    next_retry_at: Mapped[datetime | None] = mapped_column(Timestamp)
    started_at: Mapped[datetime | None] = mapped_column(Timestamp)
    finished_at: Mapped[datetime | None] = mapped_column(Timestamp)


class TestRun(_Created, Base):
    """Summary of a sandboxed test run. Raw logs are not stored here."""

    __test__ = False  # not a pytest class
    __tablename__ = "test_runs"
    __table_args__ = (Index("ix_test_runs_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    job_id: Mapped[uuid.UUID] = _job_fk()
    framework: Mapped[str] = mapped_column(String(64))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    passed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[dict[str, Any] | None] = mapped_column(Json)  # sanitized failing test names


class Finding(_Created, Base):
    """A review finding: metadata and line references only."""

    __tablename__ = "findings"
    __table_args__ = (Index("ix_findings_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    job_id: Mapped[uuid.UUID] = _job_fk()
    agent_type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text)
    file_path: Mapped[str] = mapped_column(Text)
    line_start: Mapped[int] = mapped_column(Integer)
    line_end: Mapped[int] = mapped_column(Integer)
    suggestion: Mapped[str | None] = mapped_column(Text)  # sanitized prose, not file contents
    confidence: Mapped[float] = mapped_column(Float)
    rationale: Mapped[str | None] = mapped_column(Text)
    schema_version: Mapped[int] = mapped_column(Integer, default=1)


class LlmCall(_Created, Base):
    """Per-call LLM metadata for cost and latency accounting. No prompts or completions."""

    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    job_id: Mapped[uuid.UUID] = _job_fk()
    stage_attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("stage_attempts.id", ondelete="SET NULL"), index=True
    )
    model: Mapped[str] = mapped_column(String(128))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal(0))
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    outcome: Mapped[str] = mapped_column(String(32))  # ok, timeout, rate_limited, invalid, error


class Comment(_Created, Base):
    """A comment/review posted to GitHub, kept so reruns update instead of duplicating."""

    __tablename__ = "comments"
    __table_args__ = (
        UniqueConstraint("github_comment_id"),
        Index("ix_comments_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    job_id: Mapped[uuid.UUID] = _job_fk()
    finding_id: Mapped[int | None] = mapped_column(
        ForeignKey("findings.id", ondelete="SET NULL"), index=True
    )
    github_comment_id: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(32))  # inline | summary | review


class Event(_Created, Base):
    """Append-only timeline. Application code only inserts; T-06 enforces no update/delete.

    Rows go away only via job cascade in the retention purge.
    """

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_created_at", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    job_id: Mapped[uuid.UUID] = _job_fk()
    type: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str | None] = mapped_column(String(64))
    schema_version: Mapped[int] = mapped_column(Integer, default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(Json, default=dict)
