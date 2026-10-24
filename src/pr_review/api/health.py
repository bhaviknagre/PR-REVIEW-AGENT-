"""Liveness, readiness, Prometheus exposition and job timeline. Include `router` in the app."""

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from pr_review.core.metrics import REGISTRY, set_queue_gauges
from pr_review.core.redaction import redact_obj
from pr_review.storage.events import EventRepository
from pr_review.storage.models import Job

log = logging.getLogger(__name__)
CHECK_TIMEOUT_S = 2.0

router = APIRouter()
Checker = Callable[[], Awaitable[object]]


def db_checker(factory: "sessionmaker[Session]") -> Checker:
    def ping() -> None:
        with factory() as s:
            s.execute(text("SELECT 1"))

    return lambda: run_in_threadpool(ping)


def redis_checker(client: Any) -> Checker:
    return lambda: client.ping()


def get_session_factory(request: Request) -> "sessionmaker[Session] | None":
    return getattr(request.app.state, "session_factory", None)


def get_checkers(request: Request) -> dict[str, Checker | None]:
    st = request.app.state
    return {"db": getattr(st, "db_check", None), "redis": getattr(st, "redis_check", None)}


async def _run(check: Checker | None) -> str:
    if check is None:
        return "unconfigured"
    try:
        await asyncio.wait_for(check(), CHECK_TIMEOUT_S)
    except Exception as exc:  # status only; error text may hold connection strings
        log.warning("readiness check failed", extra={"error": type(exc).__name__})
        return "fail"
    return "ok"


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(checkers: Annotated[dict[str, Checker | None], Depends(get_checkers)]) -> Response:
    results = dict(zip(checkers, await asyncio.gather(*map(_run, checkers.values())), strict=True))
    ok = all(v == "ok" for v in results.values())
    body = {"status": "ok" if ok else "unavailable", "checks": results}
    return JSONResponse(redact_obj(body), status_code=200 if ok else 503)


def _refresh_queue_gauges(factory: "sessionmaker[Session]") -> None:
    with factory() as s:
        rows = s.execute(select(Job.status, func.count()).group_by(Job.status))
        counts = {st: n for st, n in rows}
    set_queue_gauges(counts.get("queued", 0), counts.get("dead_letter", 0))


@router.get("/metrics")
async def metrics(
    factory: Annotated["sessionmaker[Session] | None", Depends(get_session_factory)],
) -> Response:
    if factory is not None:
        try:
            await run_in_threadpool(_refresh_queue_gauges, factory)
        except Exception:  # serve last-known gauges rather than fail the scrape
            log.warning("queue gauge refresh failed")
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


def _timeline(factory: "sessionmaker[Session]", job_id: uuid.UUID) -> list[dict[str, Any]] | None:
    with factory() as s:
        if s.get(Job, job_id) is None:
            return None
        return [
            {
                "type": e.type,
                "stage": e.stage,
                "timestamp": e.created_at.isoformat(),
                "schema_version": e.schema_version,
                "payload": redact_obj(p.model_dump(mode="json")),
            }
            for e, p in EventRepository(s).timeline(job_id)
        ]


# ponytail: unauthenticated; FR-26 wants token auth, needs a Settings field (settings.py)
@router.get("/jobs/{job_id}/timeline")
async def job_timeline(
    job_id: uuid.UUID,
    factory: Annotated["sessionmaker[Session] | None", Depends(get_session_factory)],
) -> dict[str, Any]:
    if factory is None:
        raise HTTPException(503, "database not configured")
    events = await run_in_threadpool(_timeline, factory, job_id)
    if events is None:
        raise HTTPException(404, "job not found")
    return {"job_id": str(job_id), "events": events}
