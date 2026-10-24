"""Budget enforcement against a real Postgres container and real Ollama. No mocks.

Needs Docker (marker docker) and Ollama (marker llm); both are required, never silently faked.
"""

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
from fault_server import FaultServer  # noqa: E402

from pr_review.core.settings import Settings  # noqa: E402
from pr_review.llm.budget import (  # noqa: E402
    daily_spend,
    degraded_notice,
    job_spend,
    persist,
    provider_hooks,
)
from pr_review.llm.provider import BudgetExceeded, CallRecord, Provider  # noqa: E402
from pr_review.storage.models import LlmCall  # noqa: E402
from pr_review.storage.repositories import JobRepository  # noqa: E402

pytestmark = [pytest.mark.docker, pytest.mark.llm, pytest.mark.anyio]
ROOT = Path(__file__).resolve().parents[2]
OLLAMA = os.environ.get("PR_REVIEW_TEST_OLLAMA", "http://localhost:11434")
MODEL = os.environ.get("PR_REVIEW_TEST_LLM_MODEL", "ollama/qwen2.5-coder:1.5b")
PROMPT = "def add(a, b): return a - b"
MSG = [{"role": "user", "content": f"One sentence: what is wrong with this?\n{PROMPT}"}]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    if shutil.which("docker") is None or _docker("info").returncode != 0:
        pytest.skip("Docker unavailable")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    name = f"pr-review-budget-{uuid.uuid4().hex[:8]}"
    run = _docker(
        "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=pw",
        "-p", f"127.0.0.1:{port}:5432", "postgres:16-alpine",
    )  # fmt: skip
    if run.returncode != 0:
        pytest.skip(f"cannot start postgres container: {run.stderr.strip()}")
    try:
        eng = create_engine(f"postgresql+psycopg://postgres:pw@127.0.0.1:{port}/postgres")
        for _ in range(60):
            try:
                eng.connect().close()
                break
            except Exception:
                time.sleep(1)
        else:
            pytest.fail("postgres did not become ready")
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/pr_review/storage/migrations"))
        with eng.connect() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")
        yield eng
        eng.dispose()
    finally:
        _docker("rm", "-fv", name)


@pytest.fixture(scope="module")
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine)


@pytest.fixture(scope="module", autouse=True)
def _ollama_up() -> None:
    try:
        urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3).read()
    except OSError as e:
        pytest.fail(f"Ollama must be running for the budget gate: {e}")


def new_job(factory: sessionmaker[Session]) -> uuid.UUID:
    with factory() as s, s.begin():
        return JobRepository(s).create(f"o/{uuid.uuid4().hex[:6]}", 1, "abc", 1).id


def add_row(
    factory: sessionmaker[Session], job: uuid.UUID, cost: str, at: datetime | None = None
) -> None:
    """Prior paid spend, inserted via the ORM; created_at overridden to place it on a UTC day."""
    with factory() as s, s.begin():
        row = LlmCall(job_id=job, model="paid/x", cost_usd=Decimal(cost), outcome="ok")
        if at is not None:
            row.created_at = at
        s.add(row)


def test_persist_is_metadata_only(factory: sessionmaker[Session], engine: Engine) -> None:
    job = new_job(factory)
    persist(factory, CallRecord(job, MODEL, 11, 7, 0.25, 42, "ok"))
    persist(factory, CallRecord(None, MODEL, 1, 1, 9.0, 1, "ok"))  # no job: nothing stored
    with factory() as s:
        row = s.scalars(select(LlmCall).where(LlmCall.job_id == job)).one()
        assert (row.model, row.prompt_tokens, row.completion_tokens) == (MODEL, 11, 7)
        assert (float(row.cost_usd), row.latency_ms, row.outcome) == (0.25, 42, "ok")
        assert row.created_at is not None
    cols = {c["name"] for c in inspect(engine).get_columns("llm_calls")}
    assert not cols & {"prompt", "completion", "content", "messages", "text"}


def test_spend_sums_by_job_and_utc_day(factory: sessionmaker[Session]) -> None:
    j1, j2 = new_job(factory), new_job(factory)
    now = datetime.now(UTC)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    before = daily_spend(factory)
    add_row(factory, j1, "0.30", start + timedelta(seconds=1))
    add_row(factory, j1, "0.20", start + timedelta(hours=1))
    add_row(factory, j2, "0.10", start + timedelta(seconds=2))
    add_row(factory, j1, "5.00", start - timedelta(seconds=1))  # yesterday, 23:59:59 UTC
    assert job_spend(factory, j1) == pytest.approx(5.5)
    assert job_spend(factory, j2) == pytest.approx(0.1)
    assert job_spend(factory, new_job(factory)) == 0.0
    # Rows sit on a fixed UTC day, so evaluate "daily" relative to a clock inside that day.
    assert daily_spend(factory, now) - before == pytest.approx(0.6)
    # Same instant expressed in another zone gives the same UTC day.
    local = now.astimezone(timezone(timedelta(hours=14)))
    assert daily_spend(factory, local) == daily_spend(factory, now)
    # Yesterday's day sees only the 5.00 row (and earlier test rows, none before this module).
    assert daily_spend(factory, start - timedelta(hours=1)) >= 5.0


def test_degraded_notice_names_cap() -> None:
    job = degraded_notice(BudgetExceeded("per-job LLM budget exhausted"))
    day = degraded_notice(BudgetExceeded("daily LLM budget exhausted"))
    assert "job" in job and "daily" not in job and "$" not in job
    assert "daily" in day and "$" not in day


async def test_real_call_persisted_then_cap_blocks_before_any_request(
    factory: sessionmaker[Session],
) -> None:
    job = new_job(factory)
    base = dict(
        llm_review_model=MODEL,
        llm_triage_model=MODEL,
        llm_fallback_models=[],
        ollama_api_base=OLLAMA,
        llm_job_budget_usd=1.0,
        llm_daily_budget_usd=1e9,
    )
    p = Provider(Settings.model_construct(**base), **provider_hooks(factory))  # type: ignore[arg-type]
    res = await p.complete("triage", MSG, job_id=job, max_tokens=32)
    assert res.text.strip()
    with factory() as s:
        row = s.scalars(select(LlmCall).where(LlmCall.job_id == job)).one()
        assert row.outcome == "ok" and row.prompt_tokens == res.prompt_tokens
        assert PROMPT not in repr(vars(row)) and res.text not in repr(vars(row))
    assert job_spend(factory, job) == 0.0  # local model is free

    # Prior paid spend reaches the per-job cap; the next call must not reach the network.
    add_row(factory, job, "1.00")
    fault = FaultServer(OLLAMA)
    try:
        blocked = Provider(
            Settings.model_construct(**{**base, "ollama_api_base": fault.url}),  # type: ignore[arg-type]
            **provider_hooks(factory),
        )
        with pytest.raises(BudgetExceeded) as e1:
            await blocked.complete("triage", MSG, job_id=job)
        assert "job" in degraded_notice(e1.value)

        # Daily cap: set just below today's total spend.
        capped = Provider(
            Settings.model_construct(
                **{**base, "ollama_api_base": fault.url, "llm_daily_budget_usd": 0.5}
            ),  # type: ignore[arg-type]
            **provider_hooks(factory),
        )
        with pytest.raises(BudgetExceeded) as e2:
            await capped.complete("triage", MSG, job_id=new_job(factory))
        assert "daily" in degraded_notice(e2.value)
        assert fault.hits == []  # no request ever reached the server
    finally:
        fault.shutdown()
        fault.server_close()
