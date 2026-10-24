"""Provider against real Ollama and a real local fault server. No mocks.

Set PR_REVIEW_TEST_LLM_MODEL to pick the model (default ollama/qwen2.5-coder:1.5b).
Skipped only when Ollama is unreachable.
"""

import os
import sqlite3
import sys
import time
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fault_server import FaultServer
from pydantic import SecretStr

from pr_review.core.metrics import REGISTRY
from pr_review.core.settings import Settings
from pr_review.llm.provider import (
    BudgetExceeded,
    CallRecord,
    LLMResult,
    LLMUnavailable,
    Provider,
    Role,
)
from pr_review.llm.resilience import BreakerRegistry

pytestmark = [pytest.mark.llm, pytest.mark.anyio]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


OLLAMA = os.environ.get("PR_REVIEW_TEST_OLLAMA", "http://localhost:11434")
MODEL = os.environ.get("PR_REVIEW_TEST_LLM_MODEL", "ollama/qwen2.5-coder:1.5b")
SECRET = "sk-test-SUPERSECRETKEY123456"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
from scenarios import load  # noqa: E402  (varied buggy code from the scenario library)


def code(sid: str) -> str:
    s = load(sid)
    return s.files[s.findings[0].path].strip()


def msg(sid: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": f"One sentence: what is wrong with this?\n{code(sid)}"}]


@pytest.fixture(scope="module", autouse=True)
def _ollama_up() -> None:
    try:
        urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3).read()
    except OSError as e:
        pytest.skip(f"Ollama unreachable at {OLLAMA}: {e}")


@pytest.fixture
def fault() -> Iterator[FaultServer]:
    srv = FaultServer(OLLAMA)
    yield srv
    srv.shutdown()
    srv.server_close()


def settings(base: str = OLLAMA, **kw: object) -> Settings:
    return Settings.model_construct(
        **{
            "llm_review_model": MODEL,
            "llm_triage_model": MODEL,
            "llm_fallback_models": [],
            "ollama_api_base": base,
            **kw,
        }
    )


def provider(s: Settings, recs: list[CallRecord] | None = None, **kw: object) -> Provider:
    out = recs if recs is not None else []
    return Provider(s, persist=out.append, rng=lambda: 0.0, **kw)  # type: ignore[arg-type]


def tokens(model: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "pr_review_llm_tokens_total", {"model": model, "kind": "completion"}
        )
        or 0.0
    )


@pytest.mark.parametrize(
    ("role", "sid"), [("triage", "off_by_one"), ("review", "none_dereference")]
)
async def test_real_roles(role: Role, sid: str) -> None:
    recs: list[CallRecord] = []
    before = tokens(MODEL)
    r: LLMResult = await provider(settings(), recs).complete(role, msg(sid), max_tokens=64)
    assert r.text.strip() and r.model == MODEL
    assert r.prompt_tokens > 0 and r.completion_tokens > 0 and r.cost_usd == 0.0
    assert r.latency_ms > 0
    print(f"{role} {MODEL} latency_ms={r.latency_ms}")
    assert len(recs) == 1 and recs[0].outcome == "ok" and recs[0].latency_ms == r.latency_ms
    assert code(sid) not in repr(recs) and r.text not in repr(recs)  # metadata only
    assert tokens(MODEL) >= before + r.completion_tokens  # metrics updated


async def test_real_fallback_from_missing_model() -> None:
    recs: list[CallRecord] = []
    s = settings(llm_review_model="ollama/does-not-exist:0b", llm_fallback_models=[MODEL])
    r = await provider(s, recs).complete("review", msg("mutable_default"), max_tokens=32)
    assert r.model == MODEL and r.text.strip()
    assert [x.outcome for x in recs] == ["error", "ok"]


async def test_fault_429_retry_after_then_success(fault: FaultServer) -> None:
    fault.scripts["f:0b"] = ["429", "ok"]
    s = settings(fault.url, llm_review_model="ollama/f:0b")
    t = time.monotonic()
    r = await provider(s).complete("review", msg("sql_injection"))
    assert r.text == "scripted ok" and fault.hit_count("f:0b") == 2
    assert time.monotonic() - t >= 1.0  # Retry-After: 1 is the floor (jitter rng is 0)


async def test_fault_500_retried_then_fallback(fault: FaultServer) -> None:
    fault.scripts["f:0b"] = ["500"]
    recs: list[CallRecord] = []
    s = settings(fault.url, llm_review_model="ollama/f:0b", llm_fallback_models=[MODEL])
    r = await provider(s, recs).complete("review", msg("resource_leak"), max_tokens=32)
    assert r.model == MODEL and fault.hit_count("f:0b") == 4
    assert [x.outcome for x in recs] == ["error"] * 4 + ["ok"]


async def test_fault_hang_times_out_then_fallback(fault: FaultServer) -> None:
    fault.scripts["f:0b"] = ["hang"]
    recs: list[CallRecord] = []
    s = settings(fault.url, llm_review_model="ollama/f:0b", llm_fallback_models=[MODEL])
    r = await provider(s, recs).complete("review", msg("n_plus_one"), max_tokens=32, timeout=3)
    assert r.model == MODEL
    assert [x.outcome for x in recs[:4]] == ["timeout"] * 4 and recs[-1].outcome == "ok"


async def test_all_retries_exhausted_is_unavailable_without_secrets(fault: FaultServer) -> None:
    fault.scripts["f:0b"] = ["500"]
    s = settings(
        fault.url,
        llm_review_model="ollama/f:0b",
        llm_api_key=SecretStr(SECRET),
    )
    with pytest.raises(LLMUnavailable) as e:
        await provider(s).complete("review", msg("quadratic_loop"))
    assert fault.hit_count("f:0b") == 4
    assert SECRET not in str(e.value) and "sk-" not in str(e.value)


async def test_budget_refusal_with_real_spend_callables(tmp_path: Path, fault: FaultServer) -> None:
    db = sqlite3.connect(tmp_path / "spend.db")
    db.execute("create table llm_calls (job_id text, cost real)")
    job = uuid.uuid4()
    db.execute("insert into llm_calls values (?, ?)", (str(job), 1.5))
    db.commit()

    def job_spend(j: uuid.UUID) -> float:
        return float(
            db.execute(
                "select coalesce(sum(cost),0) from llm_calls where job_id=?", (str(j),)
            ).fetchone()[0]
        )

    def daily_spend() -> float:
        return float(db.execute("select coalesce(sum(cost),0) from llm_calls").fetchone()[0])

    s = settings(fault.url, llm_job_budget_usd=1.0, llm_daily_budget_usd=20.0)
    with pytest.raises(BudgetExceeded):
        await provider(s, job_spend=job_spend, daily_spend=daily_spend).complete(
            "review", msg("hardcoded_secret"), job_id=job
        )
    s = settings(fault.url, llm_job_budget_usd=9.0, llm_daily_budget_usd=1.0)
    with pytest.raises(BudgetExceeded):
        await provider(s, job_spend=job_spend, daily_spend=daily_spend).complete(
            "review", msg("hardcoded_secret"), job_id=job
        )
    assert fault.hits == []  # refused before any request was made


async def test_breaker_opens_then_provider_skips_faulty_model(fault: FaultServer) -> None:
    fault.scripts["f:0b"] = ["500"]
    s = settings(fault.url, llm_review_model="ollama/f:0b", llm_fallback_models=[MODEL])
    p = provider(s, breakers=BreakerRegistry(threshold=2))
    for _ in range(2):  # each call exhausts 4 attempts = one breaker failure
        assert (await p.complete("review", msg("missing_auth"), max_tokens=16)).model == MODEL
    assert fault.hit_count("f:0b") == 8
    r = await p.complete("review", msg("missing_auth"), max_tokens=16)
    assert r.model == MODEL and r.text.strip()
    assert fault.hit_count("f:0b") == 8  # open breaker: no new request reached the faulty model

    only = settings(fault.url, llm_review_model="ollama/f:0b")  # all models open -> fast failure
    t = time.monotonic()
    with pytest.raises(LLMUnavailable):
        await provider(only, breakers=p._breakers).complete("review", msg("missing_auth"))
    assert time.monotonic() - t < 0.05 and fault.hit_count("f:0b") == 8
