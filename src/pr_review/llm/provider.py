"""Provider-agnostic LLM calls via LiteLLM (FR-18): role -> model, fallback chain, retry, budgets.

Egress goes only to the configured provider (NFR-6): telemetry, callbacks and the remote model-cost
map fetch are disabled. Only call metadata is recorded, never prompts or completions (NFR-1).
"""

import os

# Must be set before litellm is imported: it fetches the model-cost map from GitHub at import.
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["LITELLM_TELEMETRY"] = "False"

import asyncio  # noqa: E402
import logging  # noqa: E402
import random  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from collections.abc import Callable  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from typing import Any, Literal  # noqa: E402

import litellm  # noqa: E402

from pr_review.core.metrics import record_llm_call  # noqa: E402
from pr_review.core.redaction import redact  # noqa: E402
from pr_review.core.settings import Settings  # noqa: E402
from pr_review.llm.resilience import BreakerRegistry  # noqa: E402
from pr_review.pipeline.retry import MAX_HINT_SECONDS, retry_after  # noqa: E402

# Prompts reach LiteLLM's loggers at DEBUG/INFO; pin them to WARNING and cut propagation so no
# app/root log level can ever print prompt content (NFR-5).
litellm.set_verbose = False  # type: ignore[attr-defined]
for _name in ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy"):
    logging.getLogger(_name).setLevel(logging.WARNING)
    logging.getLogger(_name).propagate = False

litellm.drop_params = True
litellm.success_callback = []
litellm.failure_callback = []
litellm.callbacks = []
litellm.turn_off_message_logging = True
litellm.suppress_debug_info = True
if hasattr(litellm, "telemetry"):  # removed in newer releases; harmless where it still exists
    litellm.telemetry = False

Role = Literal["review", "triage"]
TIMEOUT_S = 120.0
MAX_TOKENS: dict[str, int] = {"review": 4096, "triage": 1024}
MAX_RETRIES = 3  # retries per model, so up to 4 attempts
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0


class LLMError(Exception):
    """Base error; the message is always redacted."""


class BudgetExceeded(LLMError):
    """Per-job or daily cost cap reached; permanent for this call (do not retry)."""


class LLMUnavailable(LLMError):
    """Every model in the chain failed; transient, the pipeline may retry the stage."""

    status_code = 503


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int


@dataclass(frozen=True)
class CallRecord:
    """Metadata persisted to llm_calls. Deliberately has no prompt/completion field."""

    job_id: uuid.UUID | None
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    latency_ms: int
    outcome: str  # ok, timeout, rate_limited, error


def _status(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def _retryable(exc: BaseException) -> bool:
    code = _status(exc)
    if code is not None:
        return code in (408, 429) or code >= 500
    return isinstance(
        exc, litellm.exceptions.Timeout | litellm.exceptions.APIConnectionError | TimeoutError
    )


def _hint(exc: BaseException) -> float:
    """Retry-After seconds. LiteLLM wraps errors with a synthetic response, so the real header
    only survives in ``litellm_response_headers``."""
    hint = retry_after(exc)
    if hint is not None:
        return hint
    raw = (getattr(exc, "litellm_response_headers", None) or {}).get("retry-after")
    try:
        return max(0.0, min(float(str(raw)), MAX_HINT_SECONDS))
    except (TypeError, ValueError):
        return 0.0


def _outcome(exc: BaseException) -> str:
    if isinstance(exc, litellm.exceptions.Timeout | TimeoutError):
        return "timeout"
    return "rate_limited" if _status(exc) == 429 else "error"


class Provider:
    def __init__(
        self,
        settings: Settings,
        *,
        daily_spend: Callable[[], float] = lambda: 0.0,
        job_spend: Callable[[uuid.UUID], float] = lambda _job: 0.0,
        persist: Callable[[CallRecord], None] = lambda _rec: None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        breakers: BreakerRegistry | None = None,
    ) -> None:
        self._s = settings
        self._daily_spend = daily_spend  # sums llm_calls for today; injected, never in-memory
        self._job_spend = job_spend
        self._persist = persist
        self._sleep = sleep
        self._rng = rng
        # One breaker per model, per worker process. A model counts as one failure per complete()
        # call once its retries are exhausted, not once per attempt.
        self._breakers = breakers or BreakerRegistry()

    def models_for(self, role: Role) -> list[str]:
        primary = self._s.llm_review_model if role == "review" else self._s.llm_triage_model
        return [primary, *self._s.llm_fallback_models]

    def _kwargs(self, model: str) -> dict[str, Any]:
        if model.startswith(("ollama/", "ollama_chat/")):
            return {"api_base": self._s.ollama_api_base}
        key = self._s.llm_api_key
        return {"api_key": key.get_secret_value()} if key else {}

    def _err(self, msg: str) -> str:
        key = self._s.llm_api_key
        if key:
            msg = msg.replace(key.get_secret_value(), "[REDACTED]")
        return redact(msg)[:300]

    async def complete(
        self,
        role: Role,
        messages: list[dict[str, str]],
        *,
        job_id: uuid.UUID | None = None,
        max_tokens: int | None = None,
        timeout: float = TIMEOUT_S,
    ) -> LLMResult:
        if job_id is not None and self._job_spend(job_id) >= self._s.llm_job_budget_usd:
            raise BudgetExceeded("per-job LLM budget exhausted")
        if self._daily_spend() >= self._s.llm_daily_budget_usd:
            raise BudgetExceeded("daily LLM budget exhausted")
        last = "no models configured"
        for model in self.models_for(role):
            if not self._breakers.allow(model):
                last = f"circuit open for {model}"
                continue
            retryable_failure = False
            for attempt in range(MAX_RETRIES + 1):
                start = time.monotonic()
                try:
                    resp = await litellm.acompletion(
                        model=model,
                        messages=messages,
                        max_tokens=max_tokens or MAX_TOKENS[role],
                        timeout=timeout,
                        **self._kwargs(model),
                    )
                except Exception as exc:
                    ms = int((time.monotonic() - start) * 1000)
                    self._persist(CallRecord(job_id, model, 0, 0, 0.0, ms, _outcome(exc)))
                    last = self._err(f"{type(exc).__name__}: {exc}")
                    if not _retryable(exc):
                        # e.g. auth/bad request: try the next model, no point retrying. The
                        # endpoint answered, so it is healthy as far as the breaker cares.
                        retryable_failure = False
                        self._breakers.record_success(model)
                        break
                    retryable_failure = True
                    if attempt < MAX_RETRIES:
                        delay = self._rng() * min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2**attempt)
                        await self._sleep(max(delay, _hint(exc)))
                    continue
                ms = int((time.monotonic() - start) * 1000)
                self._breakers.record_success(model)
                return self._finish(resp, model, job_id, ms)
            if retryable_failure:
                self._breakers.record_failure(model)
        raise LLMUnavailable(f"all models failed; last error: {last}")

    def _finish(self, resp: Any, model: str, job_id: uuid.UUID | None, ms: int) -> LLMResult:
        usage = getattr(resp, "usage", None)
        pt = int(getattr(usage, "prompt_tokens", 0) or 0)
        ct = int(getattr(usage, "completion_tokens", 0) or 0)
        cost = 0.0
        if not model.startswith(("ollama/", "ollama_chat/")):
            try:
                cost = float(litellm.completion_cost(completion_response=resp) or 0.0)
            except Exception:  # unknown pricing: record 0 rather than fail a paid-for call
                cost = 0.0
        text = resp.choices[0].message.content or ""
        record_llm_call(model, pt, ct, cost)
        self._persist(CallRecord(job_id, model, pt, ct, cost, ms, "ok"))
        return LLMResult(text, model, pt, ct, cost, ms)
