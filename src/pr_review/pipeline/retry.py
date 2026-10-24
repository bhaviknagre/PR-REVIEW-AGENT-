"""Stage retry policy (FR-23): classify the error, back off with full jitter, or dead-letter.

The worker calls ``handle_failure`` once per failed stage attempt, with the attempt row from
``state.start_stage``. A retry moves the job running -> failed -> queued and returns the delay to
pass as ARQ ``_defer_by``; if that enqueue is lost the sweeper re-enqueues the queued job, so
nothing is lost. Calling it twice for the same attempt is a no-op (the attempt is no longer
``running``), so counts and metrics are never doubled.
"""

import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy.orm import Session

from pr_review.connectors.github.auth import Forbidden, NotFound, Unauthorized, Unavailable
from pr_review.connectors.github.client import DiffTooLarge, RateLimited
from pr_review.core.metrics import DLQ_SIZE, record_job_finished
from pr_review.pipeline import state
from pr_review.runner.workspace import (
    GitTimeoutError,
    HeadMismatchError,
    WorkspaceTooLargeError,
)
from pr_review.storage.models import StageAttempt
from pr_review.storage.sanitize import sanitize_text

MAX_MESSAGE = 200  # error text kept in records; long messages risk carrying source/diff text
MAX_HINT_SECONDS = 3600.0


class LeaseLost(Exception):
    """The worker's lease expired mid-stage; another worker may take over, so retry."""


# (max attempts, backoff base seconds, backoff cap seconds). SPEC gives no numbers (FR-23).
DEFAULT_POLICY = (3, 2.0, 60.0)
POLICIES: dict[str, tuple[int, float, float]] = {
    "fetch": (5, 1.0, 60.0),
    "detect": (3, 1.0, 30.0),
    "test": (2, 5.0, 120.0),  # sandbox runs are expensive
    "parse": (2, 1.0, 30.0),
    "analyse": (4, 2.0, 120.0),  # LLM rate limits
    "aggregate": (3, 1.0, 30.0),
    "post": (5, 2.0, 60.0),
}

RETRYABLE = (
    TimeoutError,
    ConnectionError,
    httpx.TimeoutException,
    httpx.TransportError,
    Unavailable,
    RateLimited,
    GitTimeoutError,
    LeaseLost,
)
PERMANENT = (
    ValueError,  # includes pydantic validation errors
    Unauthorized,
    Forbidden,
    NotFound,
    DiffTooLarge,
    WorkspaceTooLargeError,
    HeadMismatchError,
    state.TransitionError,
)


def _status(exc: BaseException) -> int | None:
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else None


def is_retryable(exc: BaseException) -> bool:
    """HTTP status wins (5xx/429 retry, other 4xx do not), then the explicit lists. Unknown
    errors retry: attempts are bounded, so a mistaken retry costs little and a mistaken
    dead-letter loses a review."""
    code = _status(exc)
    if code is not None:
        return code >= 500 or code == 429
    if isinstance(exc, RETRYABLE):
        return True
    return not isinstance(exc, PERMANENT)


def retry_after(exc: BaseException) -> float | None:
    """Seconds from a ``retry_after`` attribute or a ``Retry-After`` response header."""
    hint = getattr(exc, "retry_after", None)
    if hint is None:
        headers = getattr(getattr(exc, "response", None), "headers", None)
        hint = headers.get("Retry-After") if headers is not None else None
    try:
        return max(0.0, min(float(hint), MAX_HINT_SECONDS)) if hint is not None else None
    except (TypeError, ValueError):
        return None


def backoff_delay(
    stage: str, attempt_no: int, hint: float | None = None, rng: Callable[[], float] = random.random
) -> float:
    """Full jitter: uniform(0, min(cap, base * 2**(attempt-1))); a server hint is a floor."""
    _, base, cap = POLICIES.get(stage, DEFAULT_POLICY)
    delay = rng() * min(cap, base * 2 ** (attempt_no - 1))
    return float(max(delay, hint)) if hint else delay


def sanitize(exc: BaseException) -> str:
    return sanitize_text(f"{type(exc).__name__}: {exc}", MAX_MESSAGE)


@dataclass(frozen=True)
class Decision:
    action: str  # "retry" | "dead_letter" | "stopped" | "duplicate"
    delay: float = 0.0  # seconds; pass as ARQ _defer_by when action == "retry"
    reason: str = ""


def handle_failure(
    session: Session,
    job_id: uuid.UUID,
    attempt: StageAttempt,
    exc: BaseException,
    duration_ms: int,
    *,
    rng: Callable[[], float] = random.random,
    now: datetime | None = None,
) -> Decision:
    session.refresh(attempt)
    if attempt.status != "running":
        return Decision("duplicate")
    stage, n = attempt.stage, attempt.attempt_no
    error = sanitize(exc)
    retry = is_retryable(exc) and n < POLICIES.get(stage, DEFAULT_POLICY)[0]
    delay = backoff_delay(stage, n, retry_after(exc), rng) if retry else 0.0
    state.finish_stage(session, attempt, error, duration_ms)
    try:
        if retry:
            state.transition(session, job_id, state.RUNNING, state.FAILED, error)
            attempt.next_retry_at = (now or datetime.now(UTC)) + timedelta(seconds=delay)
            state.transition(
                session, job_id, state.FAILED, state.QUEUED, f"retry {stage} in {delay:.1f}s"
            )
            return Decision("retry", delay, error)
        why = "attempts exhausted" if is_retryable(exc) else "permanent error"
        reason = f"stage {stage} {why} after {n} attempt(s): {error}"
        state.transition(session, job_id, state.RUNNING, state.DEAD_LETTER, reason)
    except state.TransitionError:  # superseded, cancelled or lease taken: that outcome stands
        return Decision("stopped")
    DLQ_SIZE.inc()
    record_job_finished(state.DEAD_LETTER)
    return Decision("dead_letter", reason=reason)
