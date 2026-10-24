"""ARQ worker. Redis/ARQ only delivers job ids; the Postgres lease (state.claim) is the guard, so
a lost or duplicated message can never cause double processing."""

import asyncio
import logging
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from arq import cron
from arq.connections import ArqRedis, RedisSettings, create_pool
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from pr_review.core.metrics import observe_stage, record_job_finished, set_queue_gauges
from pr_review.core.settings import WorkerConfig
from pr_review.pipeline import lease, retry, state
from pr_review.pipeline.supersede import is_cancelled
from pr_review.storage.models import Job
from pr_review.storage.retention import run_retention

log = logging.getLogger(__name__)

Stage = Callable[[uuid.UUID], Awaitable[None]]
# Failure hook: (job_id, stage, reason) after a job is dead-lettered; posts the partial summary.
OnFailure = Callable[[uuid.UUID, str, str], Awaitable[None]]
# Filled by on_startup with pipeline.stages.Pipeline.handlers(); run_job takes any mapping.
STAGE_HANDLERS: dict[str, Stage] = {}
FAILURE_POST_TIMEOUT_S = 120.0

CFG = WorkerConfig()
STALE_QUEUED_AFTER = timedelta(seconds=120)
SWEEP_BATCH = 100


class ArqEnqueuer:
    """Implements the webhook's Enqueuer protocol. ARQ _job_id makes re-enqueue idempotent."""

    def __init__(self, pool: ArqRedis) -> None:
        self._pool = pool

    async def enqueue(self, job_id: uuid.UUID, defer_by: float | None = None) -> None:
        if defer_by is None:
            await self._pool.enqueue_job("process_job", str(job_id), _job_id=str(job_id))
        else:  # retry from inside the running job: its own _job_id is still in progress in ARQ
            await self._pool.enqueue_job("process_job", str(job_id), _defer_by=defer_by)


async def run_job(
    factory: sessionmaker[Session],
    stages: dict[str, Stage],
    job_id: uuid.UUID,
    owner: str,
    cfg: WorkerConfig = CFG,
    enqueue: lease.Enqueue | None = None,
    clock: Callable[[], datetime] = lease.utcnow,
    on_failure: OnFailure | None = None,
) -> str:
    """Claim and run the remaining stages. Returns the job status or 'skipped' if not claimed.
    Retries re-enqueue via ``enqueue``; without it the stale-queued sweeper picks the job up."""
    ttl = timedelta(seconds=cfg.worker_lease_seconds)
    # ponytail: sync DB calls on the event loop; they are short, move to a thread pool if needed.
    with factory() as session:
        if not state.claim(session, job_id, owner, ttl):
            log.info("job not claimable, skipping", extra={"job": str(job_id)})
            return "skipped"
        lost = asyncio.Event()

        async def beat() -> None:
            while True:
                await asyncio.sleep(ttl.total_seconds() / 3)
                with factory() as s:
                    if not state.heartbeat(s, job_id, owner, ttl):
                        lost.set()
                        return

        beater = asyncio.create_task(beat())
        try:
            return await _run_stages(session, stages, job_id, lost, cfg, enqueue, clock, on_failure)
        finally:
            beater.cancel()


async def _run_stages(
    session: Session,
    stages: dict[str, Stage],
    job_id: uuid.UUID,
    lost: asyncio.Event,
    cfg: WorkerConfig,
    enqueue: lease.Enqueue | None,
    clock: Callable[[], datetime],
    on_failure: OnFailure | None = None,
) -> str:
    done = state.completed_stages(session, job_id)
    for name in state.STAGES:
        if name in done:
            continue
        if lost.is_set() or is_cancelled(session, job_id):
            return "stopped"
        remaining = lease.deadline_remaining(session, job_id, cfg, clock())
        if remaining <= 0:
            outcome = lease.dead_letter_deadline(session, job_id)
            if outcome == state.DEAD_LETTER:
                await _report_failure(on_failure, job_id, name, lease.DEADLINE_REASON)
            return outcome
        started = time.perf_counter()
        attempt = state.start_stage(session, job_id, name)
        try:
            handler = stages.get(name)
            if handler is None:
                raise LookupError(f"no handler registered for stage {name!r}")
            async with asyncio.timeout(lease.stage_timeout(name, cfg, remaining)):
                await handler(job_id)
        except Exception as exc:
            elapsed = time.perf_counter() - started
            observe_stage(name, elapsed)
            decision = retry.handle_failure(session, job_id, attempt, exc, int(elapsed * 1000))
            if decision.action == "dead_letter":
                await _report_failure(on_failure, job_id, name, decision.reason)
                return state.DEAD_LETTER
            if decision.action != "retry":
                return "stopped"
            if enqueue is not None:
                try:
                    await enqueue(job_id, decision.delay)
                except Exception:  # job is queued in Postgres; the sweeper re-enqueues it
                    log.exception("retry enqueue failed", extra={"job": str(job_id)})
            return state.QUEUED
        elapsed = time.perf_counter() - started
        observe_stage(name, elapsed)
        state.finish_stage(session, attempt, None, int(elapsed * 1000))
    return _finish(session, job_id, state.SUCCEEDED)


async def _report_failure(
    on_failure: OnFailure | None, job_id: uuid.UUID, stage: str, reason: str
) -> None:
    """Best effort: the job is already dead-lettered, so a failed summary post is only logged."""
    if on_failure is None:
        return
    try:
        async with asyncio.timeout(FAILURE_POST_TIMEOUT_S):
            await on_failure(job_id, stage, reason)
    except Exception:
        log.exception("failure summary not posted", extra={"job": str(job_id), "stage": stage})


def _finish(session: Session, job_id: uuid.UUID, status: str, reason: str = "") -> str:
    try:
        state.transition(session, job_id, state.RUNNING, status, reason)
    except state.TransitionError:  # superseded/cancelled while running: that outcome stands
        return "stopped"
    record_job_finished(status)
    return status


async def process_job(ctx: dict[str, Any], job_id: str) -> str:
    async def enqueue(jid: uuid.UUID, delay: float) -> None:
        await ArqEnqueuer(ctx["redis"]).enqueue(jid, delay)

    return await run_job(
        ctx["session_factory"],
        STAGE_HANDLERS,
        uuid.UUID(job_id),
        ctx["owner"],
        enqueue=enqueue,
        on_failure=ctx.get("on_failure"),
    )


async def sweep_stale_queued(ctx: dict[str, Any]) -> int:
    """Re-enqueue jobs left queued and never leased (enqueue lost), then recover running jobs
    whose lease expired (lease.recover_expired). Returns the number re-enqueued as stale."""
    cutoff = datetime.now(UTC) - STALE_QUEUED_AFTER
    with ctx["session_factory"]() as session:
        ids = list(
            session.scalars(
                select(Job.id)
                .where(Job.status == state.QUEUED, Job.leased_by.is_(None), Job.updated_at < cutoff)
                .limit(SWEEP_BATCH)
            )
        )
        dead = session.scalar(
            select(func.count()).select_from(Job).where(Job.status == state.DEAD_LETTER)
        )
        set_queue_gauges(len(ids), dead or 0)
    enqueuer = ArqEnqueuer(ctx["redis"])
    for job_id in ids:
        await enqueuer.enqueue(job_id)
    await lease.recover_expired(
        ctx["session_factory"], enqueuer.enqueue, CFG, ctx.get("owner", "sweeper")
    )
    return len(ids)


async def retention_job(ctx: dict[str, Any]) -> None:
    """Daily purge of old records and stale workspaces (FR-25)."""
    settings = ctx["settings"]
    root = Path(settings.workspace_root) if settings.workspace_root else None
    await asyncio.to_thread(run_retention, ctx["session_factory"], settings, workspace_root=root)


async def on_startup(ctx: dict[str, Any]) -> None:
    """One DB engine, HTTP client, breaker registry and LLM Provider per worker process, reused
    by every job. Redis is ARQ's own connection (ctx["redis"])."""
    from pr_review.core.redaction import register_settings
    from pr_review.core.settings import get_settings
    from pr_review.llm.budget import provider_hooks
    from pr_review.llm.provider import Provider
    from pr_review.llm.resilience import BreakerRegistry
    from pr_review.pipeline.stages import Pipeline

    settings = get_settings()
    register_settings(settings)
    engine = create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
    factory = sessionmaker(engine, expire_on_commit=False)
    http = httpx.AsyncClient()
    provider = Provider(settings, breakers=BreakerRegistry(), **provider_hooks(factory))
    pipeline = Pipeline(settings, factory, provider, http)
    STAGE_HANDLERS.update(pipeline.handlers())
    ctx.update(
        settings=settings,
        engine=engine,
        http=http,
        provider=provider,
        session_factory=factory,
        on_failure=pipeline.post_failure,
        owner=f"{socket.gethostname()}:{os.getpid()}",
    )


async def on_shutdown(ctx: dict[str, Any]) -> None:
    if "http" in ctx:
        await ctx["http"].aclose()
    if "engine" in ctx:
        ctx["engine"].dispose()


def redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(os.environ.get("REDIS_URL", "redis://localhost:6379"))


async def create_enqueuer() -> ArqEnqueuer:
    return ArqEnqueuer(await create_pool(redis_settings()))


class WorkerSettings:
    functions = [process_job]
    cron_jobs = [
        cron(sweep_stale_queued, second=set(range(0, 60, CFG.sweep_interval_seconds))),
        cron(retention_job, hour=CFG.retention_hour_utc, minute=0, second=0, timeout=3600),
    ]
    timezone = UTC
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = redis_settings()
    max_jobs = CFG.worker_max_jobs
    job_timeout = CFG.job_deadline_seconds  # arq default (300 s) would cut stages short
    keep_result = 0
