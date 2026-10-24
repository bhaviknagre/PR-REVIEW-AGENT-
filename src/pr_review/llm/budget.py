"""DB-backed LLM budgets (FR-18, NFR-15): spend lookups and call-metadata persistence for Provider.

Spend is always summed from llm_calls, never held in memory, so it survives restarts and is shared
by every worker. Only metadata is stored (FR-24, NFR-1); CallRecord has no prompt/completion field.

Concurrency: Provider checks the cap before a call and the cost is only known after it, so with N
concurrent workers the cap can be overshot by at most one in-flight call per worker (N calls).
No reservation is held; that bound is the accepted trade-off for a lock-free check.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TypedDict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pr_review.llm.provider import BudgetExceeded, CallRecord
from pr_review.storage.models import LlmCall

SessionFactory = Callable[[], Session]


class Hooks(TypedDict):
    daily_spend: Callable[[], float]
    job_spend: Callable[[uuid.UUID], float]
    persist: Callable[[CallRecord], None]


def job_spend(factory: SessionFactory, job_id: uuid.UUID) -> float:
    with factory() as s:
        total = s.scalar(
            select(func.coalesce(func.sum(LlmCall.cost_usd), 0)).where(LlmCall.job_id == job_id)
        )
    return float(total or 0)


def daily_spend(factory: SessionFactory, now: datetime | None = None) -> float:
    """Spend since 00:00 UTC today. The half-open range on created_at uses its index."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with factory() as s:
        total = s.scalar(
            select(func.coalesce(func.sum(LlmCall.cost_usd), 0)).where(
                LlmCall.created_at >= start, LlmCall.created_at < start + timedelta(days=1)
            )
        )
    return float(total or 0)


def persist(factory: SessionFactory, record: CallRecord) -> None:
    """Insert one metadata row. Records without a job cannot be stored (job_id is NOT NULL)."""
    if record.job_id is None:
        return
    with factory() as s, s.begin():
        s.add(
            LlmCall(
                job_id=record.job_id,
                model=record.model,
                prompt_tokens=record.prompt_tokens,
                completion_tokens=record.completion_tokens,
                cost_usd=record.cost_usd,
                latency_ms=record.latency_ms,
                outcome=record.outcome,
            )
        )


def provider_hooks(factory: SessionFactory) -> Hooks:
    """Provider() keyword args (daily_spend, job_spend, persist) bound to a session factory."""
    return Hooks(
        daily_spend=lambda: daily_spend(factory),
        job_spend=lambda job_id: job_spend(factory, job_id),
        persist=lambda rec: persist(factory, rec),
    )


def degraded_notice(exc: BudgetExceeded) -> str:
    """Short PR-summary text naming the cap hit; no amounts or cross-tenant figures."""
    if "daily" in str(exc):
        return (
            "Review degraded: the daily LLM cost cap was reached, so AI analysis was skipped. "
            "Static checks still ran; re-run after the cap resets (00:00 UTC)."
        )
    return (
        "Review degraded: this job's LLM budget was reached, so remaining AI analysis was "
        "skipped. Results so far are shown."
    )
