"""Retention purge (FR-25): old terminal jobs (children and events go by FK cascade), delivery
keys and leftover workspaces.

Rules:
- A job is purged once it is terminal and ``coalesce(finished_at, updated_at)`` is older than
  ``retention_days``. queued/running jobs are never touched, whatever their age.
- ``stage_attempts``, ``test_runs``, ``findings``, ``llm_calls``, ``comments`` and ``events`` die
  through ``ON DELETE CASCADE`` at the database level; Event rows are never ORM-deleted.
- A delivery key is purged after ``max(retention_days, 7)`` days: GitHub lets operators redeliver
  for a few days, and a purged key would let a redelivery create a duplicate review.
- Leftover workspace dirs older than ``workspace_max_age_s`` (default max(2 x job deadline, 2h))
  are removed via ``cleanup_stale``.
- No raw-log/artifact directory setting exists (logs go to stdout, test runs keep summaries only),
  so there are no log files to purge.
- Deletes run in bounded batches, one commit each. A Postgres advisory lock lets a second
  concurrent run skip; SQLite (single-writer) has no lock and relies on idempotent deletes.
- Only counts are logged.
"""

import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

from prometheus_client import Counter
from sqlalchemy import ColumnElement, delete, func, select, text
from sqlalchemy.orm import Session

from pr_review.core.metrics import REGISTRY
from pr_review.core.settings import Settings
from pr_review.runner.workspace import cleanup_stale
from pr_review.storage.models import (
    Comment,
    DeliveryKey,
    Event,
    Finding,
    Job,
    LlmCall,
    StageAttempt,
    TestRun,
)

log = logging.getLogger(__name__)

TERMINAL = ("succeeded", "failed", "dead_letter", "superseded", "cancelled")
DELIVERY_KEY_FLOOR_DAYS = 7
LOCK_KEY = 0x70727265  # arbitrary constant: "prre"

PURGED = Counter(
    "pr_review_retention_purged_total", "Rows purged by retention", ["table"], registry=REGISTRY
)

_CHILDREN = (StageAttempt, TestRun, Finding, LlmCall, Comment, Event)


@dataclass
class RetentionReport:
    counts: dict[str, int] = field(default_factory=dict)
    workspaces_removed: int = 0
    duration_s: float = 0.0
    skipped: bool = False  # another worker holds the lock


def _batches(
    session_factory: Callable[[], Session],
    table: Any,
    pk: Any,
    where: ColumnElement[bool],
    batch_size: int,
    report: RetentionReport,
) -> None:
    while True:
        with session_factory() as s:
            ids = list(s.scalars(select(pk).where(where).limit(batch_size)))
            if not ids:
                return
            if table is Job:
                for child in _CHILDREN:
                    q = select(func.count()).select_from(child).where(child.job_id.in_(ids))
                    n = s.scalar(q)
                    report.counts[child.__tablename__] += n or 0
            res: Any = s.execute(delete(table).where(pk.in_(ids)))
            s.commit()
            report.counts[table.__tablename__] += res.rowcount or 0


def run_retention(
    session_factory: Callable[[], Session],
    settings: Settings,
    now: datetime | None = None,
    *,
    batch_size: int = 500,
    workspace_root: Path | None = None,
    workspace_max_age_s: float | None = None,
) -> RetentionReport:
    start = perf_counter()
    now = now or datetime.now(UTC)
    days = settings.retention_days
    job_cutoff = now - timedelta(days=days)
    key_cutoff = now - timedelta(days=max(days, DELIVERY_KEY_FLOOR_DAYS))
    report = RetentionReport(counts={t.__tablename__: 0 for t in (Job, DeliveryKey, *_CHILDREN)})
    lock = session_factory()
    try:
        postgres = lock.get_bind().dialect.name == "postgresql"
        if postgres and not lock.scalar(text("select pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}):
            report.skipped = True
            log.info("retention skipped: another worker holds the lock")
            return report
        try:
            _batches(
                session_factory, Job, Job.id,
                Job.status.in_(TERMINAL)
                & (func.coalesce(Job.finished_at, Job.updated_at) < job_cutoff),
                batch_size, report,
            )  # fmt: skip
            _batches(
                session_factory, DeliveryKey, DeliveryKey.delivery_id,
                DeliveryKey.created_at < key_cutoff, batch_size, report,
            )  # fmt: skip
            # A job may legitimately run until job_deadline_seconds; twice that (floor 2h) keeps
            # a workspace in use by a late job from being deleted mid-run.
            max_age = workspace_max_age_s or max(2 * settings.job_deadline_seconds, 2 * 3600)
            root = workspace_root or Path(tempfile.gettempdir())
            report.workspaces_removed = cleanup_stale(root, max_age)
        finally:
            if postgres:
                lock.execute(text("select pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
    finally:
        lock.close()
    report.duration_s = perf_counter() - start
    for table, n in report.counts.items():
        PURGED.labels(table).inc(n)
    log.info(
        "retention done",
        extra={"counts": report.counts, "workspaces": report.workspaces_removed},
    )
    return report
