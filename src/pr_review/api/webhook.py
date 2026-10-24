"""GitHub webhook ingress: size-limited, HMAC-verified on the raw body before any parsing."""

import hashlib
import hmac
import json
import logging
import uuid
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from pr_review.core.settings import Settings
from pr_review.pipeline.supersede import supersede_older
from pr_review.storage.repositories import DeliveryKeyRepository, JobRepository

log = logging.getLogger(__name__)
ACTIONS = frozenset({"opened", "synchronize", "reopened", "ready_for_review"})

MAX_BODY_BYTES = 5 * 1024 * 1024  # GitHub caps payloads at 25 MB; PR events are far smaller


async def _limit_ip(request: Request) -> None:
    limiter = getattr(request.app.state, "limiter", None)
    if limiter is not None:
        await limiter.check_ip(request)


router = APIRouter(dependencies=[Depends(_limit_ip)])


class Enqueuer(Protocol):
    """Hands a persisted job to the work queue (Redis/ARQ implementation arrives later)."""

    async def enqueue(self, job_id: uuid.UUID) -> None: ...


def get_enqueuer(request: Request) -> Enqueuer | None:
    return getattr(request.app.state, "enqueuer", None)


def get_session_factory(request: Request) -> "sessionmaker[Session] | None":
    return getattr(request.app.state, "session_factory", None)


def _persist(
    factory: "sessionmaker[Session]", delivery: str, repo: str, pr: int, sha: str, inst: int
) -> uuid.UUID | None:
    """Job + delivery key in one transaction. Returns the new job id, or None for a duplicate."""
    with factory.begin() as session:
        jobs = JobRepository(session)
        job = jobs.find(repo, pr, sha)
        if job is not None:  # same head SHA redelivered under a new delivery id
            DeliveryKeyRepository(session).record(delivery, job.id)
            return None
        job = jobs.create(repo, pr, sha, inst)
        if not DeliveryKeyRepository(session).record(delivery, job.id):
            session.rollback()
            return None
        supersede_older(session, job)
        return job.id


def parse_payload(raw: bytes) -> Any:
    return json.loads(raw)


def _verified(secret: str, header: str | None, body: bytes) -> bool:
    if header is None or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header[7:].encode(), expected.encode())


async def _read_limited(request: Request) -> bytes | None:
    """Raw body, or None if it exceeds the limit (checked up front and while streaming)."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > MAX_BODY_BYTES):
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/webhook/github", status_code=202)
async def github_webhook(
    request: Request,
    enqueuer: Annotated[Enqueuer | None, Depends(get_enqueuer)],
    factory: Annotated["sessionmaker[Session] | None", Depends(get_session_factory)],
) -> Response:
    body = await _read_limited(request)
    if body is None:
        return Response(status_code=413)
    settings: Settings = request.app.state.settings
    secret = settings.github_webhook_secret.get_secret_value()
    if not _verified(secret, request.headers.get("x-hub-signature-256"), body):
        return Response("Unauthorized", status_code=401)
    try:
        payload = parse_payload(body)
    except ValueError:
        return Response("Bad Request", status_code=400)
    event = request.headers.get("x-github-event")
    if event == "ping":
        return Response("pong", status_code=200)
    if (
        event != "pull_request"
        or not isinstance(payload, dict)
        or payload.get("action") not in ACTIONS
    ):
        return Response("ignored", status_code=202)
    delivery = request.headers.get("x-github-delivery")
    try:
        repo = payload["repository"]["full_name"]
        pr = payload["pull_request"]["number"]
        sha = payload["pull_request"]["head"]["sha"]
        inst = payload["installation"]["id"]
    except (KeyError, TypeError):
        return Response("Bad Request", status_code=400)
    if not delivery or not (
        isinstance(repo, str)
        and isinstance(sha, str)
        and isinstance(pr, int)
        and isinstance(inst, int)
    ):
        return Response("Bad Request", status_code=400)
    limiter = getattr(request.app.state, "limiter", None)
    if limiter is not None:  # only after HMAC verification: unverified bodies never pick the key
        await limiter.check_installation(inst)
    if factory is None or enqueuer is None:
        return Response("Service Unavailable", status_code=503)
    ref = {"repo": repo, "pr": pr, "action": payload["action"], "delivery": delivery}
    try:
        job_id = await run_in_threadpool(_persist, factory, delivery, repo, pr, sha, inst)
    except Exception:
        log.error("webhook persist failed", extra=ref)
        return Response("Service Unavailable", status_code=503)
    if job_id is None:
        log.info("webhook duplicate", extra=ref)
        return Response("duplicate", status_code=202)
    try:
        await enqueuer.enqueue(job_id)
    except Exception:
        # Job is durable (status queued); a re-lease sweep recovers it, so still acknowledge.
        log.error("webhook enqueue failed", extra=ref)
    log.info("webhook accepted", extra=ref)
    return Response("accepted", status_code=202)
