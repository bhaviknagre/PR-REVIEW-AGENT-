"""Pure-logic provider tests: no network, no model. Every test that performs a call lives in
tests/integration/test_llm_real.py and runs against real Ollama and a real fault server."""

import asyncio
import dataclasses
import os
import uuid

import litellm
import pytest
from pydantic import SecretStr

from pr_review.core.settings import Settings
from pr_review.llm import provider as P

KEY = "sk-test-SUPERSECRETKEY123456"


def make(**kw: object) -> Settings:
    base: dict[str, object] = dict(
        llm_review_model="ollama/big", llm_triage_model="ollama/small", llm_fallback_models=["x/y"]
    )
    return Settings.model_construct(**{**base, **kw})


def test_role_to_model_mapping_with_fallbacks() -> None:
    p = P.Provider(make())
    assert p.models_for("review") == ["ollama/big", "x/y"]
    assert p.models_for("triage") == ["ollama/small", "x/y"]


def test_key_never_passed_to_ollama_only_to_others() -> None:
    p = P.Provider(make(llm_api_key=SecretStr(KEY), ollama_api_base="http://h:1"))
    assert p._kwargs("ollama/big") == {"api_base": "http://h:1"}
    assert p._kwargs("ollama_chat/big") == {"api_base": "http://h:1"}
    assert p._kwargs("openai/gpt-4o-mini") == {"api_key": KEY}
    assert P.Provider(make())._kwargs("openai/gpt-4o-mini") == {}


def test_error_text_redacts_key() -> None:
    p = P.Provider(make(llm_api_key=SecretStr(KEY)))
    assert KEY not in p._err(f"auth failed for {KEY}")


def test_backoff_envelope_and_attempt_budget() -> None:
    ceilings = [min(P.BACKOFF_CAP_S, P.BACKOFF_BASE_S * 2**a) for a in range(P.MAX_RETRIES)]
    assert ceilings == [1.0, 2.0, 4.0]
    assert P.MAX_RETRIES + 1 == 4


@pytest.mark.parametrize(
    ("status", "retry", "outcome"),
    [(429, True, "rate_limited"), (500, True, "error"), (408, True, "error"),
     (401, False, "error"), (404, False, "error")],
)  # fmt: skip
def test_status_classification(status: int, retry: bool, outcome: str) -> None:
    exc = Exception("x")
    exc.status_code = status  # type: ignore[attr-defined]
    assert P._retryable(exc) is retry
    assert P._outcome(exc) == outcome


def test_timeout_classification() -> None:
    exc = litellm.exceptions.Timeout("t", "m", "p")
    assert P._retryable(exc) and P._outcome(exc) == "timeout"
    assert P._retryable(TimeoutError()) and P._outcome(TimeoutError()) == "timeout"


@pytest.mark.parametrize(
    ("job_spend", "daily_spend"), [(1.0, 0.0), (5.0, 0.0), (0.0, 20.0), (0.0, 99.0)]
)
def test_budget_arithmetic_refuses_at_or_over_cap(job_spend: float, daily_spend: float) -> None:
    p = P.Provider(
        make(llm_job_budget_usd=1.0, llm_daily_budget_usd=20.0),
        job_spend=lambda _j: job_spend,
        daily_spend=lambda: daily_spend,
    )
    with pytest.raises(P.BudgetExceeded):  # refused before any model is contacted
        asyncio.run(p.complete("review", [], job_id=uuid.uuid4()))


def test_call_record_has_no_prompt_or_completion_field() -> None:
    names = {f.name for f in dataclasses.fields(P.CallRecord)}
    assert not names & {"content", "prompt", "completion", "text", "messages"}


def test_telemetry_and_callbacks_disabled() -> None:
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"
    assert os.environ["LITELLM_TELEMETRY"] == "False"
    assert litellm.success_callback == [] and litellm.failure_callback == []
    assert litellm.callbacks == [] and litellm.drop_params is True
    assert litellm.turn_off_message_logging is True


def test_retry_after_hint_read_from_litellm_wrapped_headers() -> None:
    exc = Exception("x")
    exc.litellm_response_headers = {"retry-after": "7"}  # type: ignore[attr-defined]
    assert P._hint(exc) == 7.0
    assert P._hint(Exception("no headers")) == 0.0
