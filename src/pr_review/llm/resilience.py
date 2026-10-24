"""Per-model circuit breaker for the LLM fallback chain (FR-18, NFR-9).

closed -> open after FAILURE_THRESHOLD consecutive retryable failures; open -> half-open once
COOLDOWN_S has passed, letting exactly one probe through; probe success closes, failure reopens.
An open model is skipped immediately so the caller moves to the next fallback without waiting.

State is per worker process (in memory, not shared across workers). Thread-safe via a lock;
safe from asyncio too because no method awaits. Retries, backoff and timeouts live in provider.py.
Wiring: the provider calls ``allow(model)`` before each attempt chain and ``record_success`` /
``record_failure`` after it (failure only for retryable errors).
"""

import logging
import threading
import time
from collections.abc import Callable

from prometheus_client import Counter

from pr_review.core.metrics import REGISTRY

FAILURE_THRESHOLD = 5
COOLDOWN_S = 30.0

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

log = logging.getLogger(__name__)
TRANSITIONS = Counter(
    "pr_review_llm_breaker_transitions_total",
    "Circuit breaker state changes",
    ["model", "state"],
    registry=REGISTRY,
)


class _Entry:
    state = CLOSED
    failures = 0
    opened_at = 0.0
    probing = False


class BreakerRegistry:
    def __init__(
        self,
        *,
        threshold: int = FAILURE_THRESHOLD,
        cooldown_s: float = COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_s
        self._clock = clock
        self._lock = threading.Lock()
        self._models: dict[str, _Entry] = {}

    def _entry(self, model: str) -> _Entry:
        return self._models.setdefault(model, _Entry())

    def _set(self, model: str, e: _Entry, state: str) -> None:
        e.state = state
        e.probing = False
        TRANSITIONS.labels(model, state).inc()
        log.warning("llm circuit breaker", extra={"model": model, "state": state})

    def state(self, model: str) -> str:
        with self._lock:
            return self._entry(model).state

    def allow(self, model: str) -> bool:
        """True if a call may go to ``model``. In half-open only one probe is admitted."""
        with self._lock:
            e = self._entry(model)
            if e.state == OPEN and self._clock() - e.opened_at >= self._cooldown:
                self._set(model, e, HALF_OPEN)
            if e.state == CLOSED:
                return True
            if e.state == HALF_OPEN and not e.probing:
                e.probing = True
                return True
            return False

    def record_success(self, model: str) -> None:
        with self._lock:
            e = self._entry(model)
            e.failures = 0
            if e.state != CLOSED:
                self._set(model, e, CLOSED)

    def record_failure(self, model: str) -> None:
        """Count a retryable failure (callers must not report auth/bad-request errors)."""
        with self._lock:
            e = self._entry(model)
            e.failures += 1
            if e.state == HALF_OPEN or (e.state == CLOSED and e.failures >= self._threshold):
                e.opened_at = self._clock()
                self._set(model, e, OPEN)
