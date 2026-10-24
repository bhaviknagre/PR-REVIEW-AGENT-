"""Circuit breaker against the real fault server and real Ollama. No mocks.

The clock is injected so cool-down needs no sleeping. Skipped only when Ollama is unreachable.
"""

import os
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import litellm
import pytest

from pr_review.llm.provider import LLMUnavailable, _retryable
from pr_review.llm.resilience import CLOSED, HALF_OPEN, OPEN, BreakerRegistry

sys.path.insert(0, str(Path(__file__).parents[1] / "integration"))
from fault_server import FaultServer  # noqa: E402

pytestmark = [pytest.mark.llm, pytest.mark.anyio]

OLLAMA = os.environ.get("PR_REVIEW_TEST_OLLAMA", "http://localhost:11434")
MODEL = os.environ.get("PR_REVIEW_TEST_LLM_MODEL", "ollama/qwen2.5-coder:1.5b")
RAW = "flaky:0b"  # the fault server sees the name without the provider prefix
BAD = f"ollama/{RAW}"
MSG = [{"role": "user", "content": "Say hi in one word."}]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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


class Clock:
    now = 1000.0

    def __call__(self) -> float:
        return self.now


async def call_chain(reg: BreakerRegistry, models: list[str], base: str) -> str:
    """One real LiteLLM call per model, gated by the breaker exactly as the provider will."""
    for model in models:
        if not reg.allow(model):
            continue
        try:
            await litellm.acompletion(
                model=model, messages=MSG, max_tokens=8, timeout=60, api_base=base
            )
        except Exception as exc:
            if _retryable(exc):
                reg.record_failure(model)
            continue
        reg.record_success(model)
        return model
    raise LLMUnavailable("all models failed or open")


async def test_open_skip_fast_fallback_probe_close_and_reopen(fault: FaultServer) -> None:
    clock = Clock()
    reg = BreakerRegistry(threshold=3, cooldown_s=30.0, clock=clock)
    fault.scripts[RAW] = ["500"]
    chain = [BAD, MODEL]
    for _ in range(3):  # N consecutive 500s open the breaker; real Ollama answers each time
        assert await call_chain(reg, chain, fault.url) == MODEL
    assert reg.state(BAD) == OPEN and fault.hit_count(RAW) == 3

    t0 = time.monotonic()
    assert await call_chain(reg, chain, fault.url) == MODEL
    assert fault.hit_count(RAW) == 3  # skipped: the faulty server never saw it
    print(f"open-skip call (incl. real Ollama) {time.monotonic() - t0:.2f}s")

    t0 = time.monotonic()
    assert not reg.allow(BAD)
    assert time.monotonic() - t0 < 0.01

    clock.now += 31  # cool-down passed; probe still failing -> reopen
    assert await call_chain(reg, chain, fault.url) == MODEL
    assert fault.hit_count(RAW) == 4 and reg.state(BAD) == OPEN
    assert not reg.allow(BAD)  # new cool-down started

    fault.scripts[RAW] = ["ok"]  # upstream recovers
    clock.now += 31
    assert await call_chain(reg, chain, fault.url) == BAD  # probe succeeds
    assert reg.state(BAD) == CLOSED and fault.hit_count(RAW) == 5


async def test_all_open_fails_fast(fault: FaultServer) -> None:
    reg = BreakerRegistry(threshold=1, cooldown_s=30.0, clock=Clock())
    fault.scripts[RAW] = ["500"]
    with pytest.raises(LLMUnavailable):
        await call_chain(reg, [BAD], fault.url)
    assert reg.state(BAD) == OPEN
    t0 = time.monotonic()
    with pytest.raises(LLMUnavailable):
        await call_chain(reg, [BAD], fault.url)
    assert time.monotonic() - t0 < 0.05 and fault.hit_count(RAW) == 1


async def test_non_retryable_does_not_trip(fault: FaultServer) -> None:
    reg = BreakerRegistry(threshold=1, clock=Clock())
    missing = "ollama/does-not-exist:0b"  # real Ollama answers 404
    assert await call_chain(reg, [missing, MODEL], OLLAMA) == MODEL
    assert reg.state(missing) == CLOSED


def test_half_open_admits_single_probe_and_success_resets_count() -> None:
    clock = Clock()
    reg = BreakerRegistry(threshold=2, cooldown_s=10.0, clock=clock)
    reg.record_failure("m")
    reg.record_success("m")  # consecutive, so the count restarts
    reg.record_failure("m")
    assert reg.state("m") == CLOSED
    reg.record_failure("m")
    assert reg.state("m") == OPEN and not reg.allow("m")
    clock.now += 10
    assert reg.allow("m") and reg.state("m") == HALF_OPEN
    assert not reg.allow("m")  # second caller is not a probe
    reg.record_success("m")
    assert reg.state("m") == CLOSED and reg.allow("m")


def test_thread_safe_single_probe() -> None:
    clock = Clock()
    reg = BreakerRegistry(threshold=1, cooldown_s=1.0, clock=clock)
    reg.record_failure("m")
    clock.now += 2
    wins: list[bool] = []
    ts = [threading.Thread(target=lambda: wins.append(reg.allow("m"))) for _ in range(32)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert wins.count(True) == 1
