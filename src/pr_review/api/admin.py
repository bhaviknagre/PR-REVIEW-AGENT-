"""Token-authenticated admin API (FR-26). Fails closed: no configured token means no access.

Query helpers take a Session and are shared with the CLI. Only metadata is returned, never
source or diff content; free text is passed through the redactor.
"""

import hmac
import logging
import uuid
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from pr_review.api.health import _timeline
from pr_review.core.redaction import redact
from pr_review.pipeline import state
from pr_review.storage.models import Job, LlmCall, StageAttempt

log = logging.getLogger(__name__)


def _iso(d: Any) -> str | None:
    return d.isoformat() if d is not None else None


def job_summary(j: Job) -> dict[str, Any]:
    return {
        "id": str(j.id),
        "repo": j.repo,
        "pr_number": j.pr_number,
        "head_sha": j.head_sha,
        "status": j.status,
        "created_at": _iso(j.created_at),
        "updated_at": _iso(j.updated_at),
        "finished_at": _iso(j.finished_at),
    }


def job_detail(s: Session, job_id: uuid.UUID) -> dict[str, Any] | None:
    """Status plus stage-attempt history (the 'attempts' of a dead-lettered job)."""
    job = s.get(Job, job_id)
    if job is None:
        return None
    rows = s.scalars(
        select(StageAttempt).where(StageAttempt.job_id == job_id).order_by(StageAttempt.id)
    )
    attempts = [
        {
            "stage": a.stage,
            "attempt_no": a.attempt_no,
            "status": a.status,
            "error": redact(a.error) if a.error else None,
            "started_at": _iso(a.started_at),
            "finished_at": _iso(a.finished_at),
        }
        for a in rows
    ]
    out = job_summary(job)
    out["dead_letter_reason"] = redact(job.dead_letter_reason) if job.dead_letter_reason else None
    out["attempts"] = attempts
    return out


def list_jobs(
    s: Session, status: str | None = None, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    q = select(Job).order_by(Job.created_at.desc(), Job.id).limit(limit).offset(offset)
    if status:
        q = q.where(Job.status == status)
    return [job_summary(j) for j in s.scalars(q)]


def dlq_list(s: Session, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    return list_jobs(s, state.DEAD_LETTER, limit, offset)


def requeue(s: Session, job_id: uuid.UUID) -> bool:
    """Operator override dead_letter -> queued via the state machine (guarded, audited).

    False if the job is absent, not dead-lettered, or another requeue won the race.
    """
    try:
        state.transition(s, job_id, state.DEAD_LETTER, state.QUEUED, "admin requeue")
    except state.TransitionError:
        return False
    return True


def cost_summary(s: Session) -> list[dict[str, Any]]:
    day = func.date(LlmCall.created_at)
    rows = s.execute(
        select(
            day,
            LlmCall.model,
            func.count(),
            func.coalesce(func.sum(LlmCall.prompt_tokens), 0),
            func.coalesce(func.sum(LlmCall.completion_tokens), 0),
            func.coalesce(func.sum(LlmCall.cost_usd), 0),
        )
        .group_by(day, LlmCall.model)
        .order_by(day.desc(), LlmCall.model)
    )
    return [
        {
            "day": str(d),
            "model": m,
            "calls": n,
            "prompt_tokens": int(p),
            "completion_tokens": int(c),
            "cost_usd": str(cost),
        }
        for d, m, n, p, c, cost in rows
    ]


def require_admin(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
    token = request.app.state.settings.admin_token
    if token is None:
        raise HTTPException(503, "admin API disabled")
    scheme, _, given = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        given.encode(), token.get_secret_value().encode()
    ):
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})


def get_session(request: Request) -> Iterator[Session]:
    factory: sessionmaker[Session] | None = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(503, "database not configured")
    with factory() as s:
        yield s


Db = Annotated[Session, Depends(get_session)]
Limit = Annotated[int, Query(ge=1, le=200)]
Offset = Annotated[int, Query(ge=0)]

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def _found(v: Any) -> Any:
    if v is None:
        raise HTTPException(404, "job not found")
    return v


@router.get("/jobs")
def jobs(db: Db, status: str | None = None, limit: Limit = 50, offset: Offset = 0) -> Any:
    return {"jobs": list_jobs(db, status, limit, offset)}


@router.get("/jobs/{job_id}")
def job(job_id: uuid.UUID, db: Db) -> Any:
    return _found(job_detail(db, job_id))


@router.get("/jobs/{job_id}/timeline")
def timeline(job_id: uuid.UUID, request: Request) -> Any:
    events = _found(_timeline(request.app.state.session_factory, job_id))
    return {"job_id": str(job_id), "events": events}


@router.get("/dlq")
def dlq(db: Db, limit: Limit = 50, offset: Offset = 0) -> Any:
    return {"jobs": dlq_list(db, limit, offset)}


@router.get("/dlq/{job_id}")
def dlq_inspect(job_id: uuid.UUID, db: Db) -> Any:
    detail = _found(job_detail(db, job_id))
    if detail["status"] != state.DEAD_LETTER:
        raise HTTPException(404, "job not in dead letter")
    return detail


@router.post("/dlq/{job_id}/requeue")
async def dlq_requeue(job_id: uuid.UUID, request: Request, db: Db) -> Any:
    if not await run_in_threadpool(requeue, db, job_id):
        raise HTTPException(404, "job not in dead letter")
    enqueuer = getattr(request.app.state, "enqueuer", None)
    if enqueuer is not None:
        try:
            await enqueuer.enqueue(job_id)
        except Exception:  # job is durable as queued; the stale-queued sweeper re-enqueues it
            log.error("admin requeue enqueue failed")
    return {"id": str(job_id), "status": state.QUEUED}


@router.get("/costs")
def costs(db: Db) -> Any:
    return {"costs": cost_summary(db)}
