"""T-45 benchmarks (NFR-14, AC-25), everything real except the two external services, which are
local HTTP servers (tests/load/stubs.py): the LLM (fixed valid review JSON, Ollama protocol) and
GitHub's REST endpoints, backed by a real local git repository.

(a) Webhook latency: >= 200 signed pull_request deliveries to the real `create_app()` (real
    Postgres + Redis), measured in-process (no network), p95 < 500 ms. Each 2xx is followed by a
    check that the job row is already committed and the id sits in the ARQ queue.
(b) Pipeline: a PR of <= 500 changed lines, built from the scenario library, goes through all seven
    stages in a real worker subprocess (fetch, detect, Docker sandbox tests, parse, LLM, aggregate,
    post) within 10 minutes and gets exactly one review.

Percentiles are printed and, when PR_REVIEW_PERF_REPORT is set, written there as JSON.
"""

# ruff: noqa: E402

import json
import math
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent / "load"), str(HERE.parent / "e2e")]

import redis as redis_sync
from scenarios import all_ids, repo_files
from sqlalchemy import func, select
from stubs import (
    REPO,
    StubGitHub,
    StubLlm,
    WorkerPool,
    build_origin,
    deliver,
    infra,
    make_stack,
    webhook_client,
    worker_env,
)

from pr_review.pipeline import state
from pr_review.storage.models import Comment, DeliveryKey, Job, StageAttempt
from pr_review.storage.repositories import JobRepository

pytestmark = [pytest.mark.perf, pytest.mark.docker]

DELIVERIES = 200
WARMUP = 5
P95_LIMIT_S = 0.5
PIPELINE_LIMIT_S = 600.0
MAX_CHANGED_LINES = 500
TARGET_LINES = 420  # comfortably inside the limit


def percentile(sorted_values: list[float], p: float) -> float:
    """Nearest-rank percentile of an already sorted list."""
    return sorted_values[max(0, math.ceil(p / 100 * len(sorted_values)) - 1)]


def _report(name: str, data: dict[str, float]) -> None:
    print(f"\n[perf] {name}: " + ", ".join(f"{k}={v:.3f}" for k, v in data.items()))
    path = os.environ.get("PR_REVIEW_PERF_REPORT")
    if path:
        with open(path, "a") as f:
            f.write(json.dumps({"name": name, **data}) + "\n")


def bench_files() -> dict[str, str]:
    """Every library scenario, then copies of the non-test source files under v1/, v2/, ... until
    the PR reaches TARGET_LINES added lines. Test files are not copied (their module names would
    collide in the sandbox's pytest run)."""
    files = repo_files(*all_ids(), requirements=False)
    sources = {p: c for p, c in files.items() if not p.rsplit("/", 1)[-1].startswith("test_")}

    def lines() -> int:
        return sum(c.count("\n") for c in files.values())

    for n in range(1, 50):
        for path, content in sources.items():
            if lines() + content.count("\n") <= TARGET_LINES:
                files[f"v{n}/{path}"] = content
        if lines() >= TARGET_LINES - 10:
            break
    return files


def test_webhook_p95_under_500ms() -> None:
    total = WARMUP + DELIVERIES
    shas = [uuid.uuid4().hex + "0" * 8 for _ in range(total)]
    with infra("perf-hook") as inf:
        st = make_stack(inf)
        timings: list[float] = []
        with webhook_client(st) as client:
            app_limiter = client.app.state.limiter  # type: ignore[attr-defined]
            # The per-IP/installation rate limits (60/120 per minute) would 429 a 200-request
            # run from one address. Lift them but keep the Redis round trips in the timed path.
            app_limiter._ip_limit = app_limiter._installation_limit = 10**9
            for i, sha in enumerate(shas):
                t0 = time.perf_counter()
                resp = deliver(client, st.secret, 1000 + i, sha)
                dt = time.perf_counter() - t0
                assert resp.status_code == 202 and resp.text == "accepted", resp.text
                with inf.factory() as s:  # durable before (or at) the 2xx, not "eventually"
                    assert JobRepository(s).find(REPO, 1000 + i, sha) is not None
                if i >= WARMUP:
                    timings.append(dt)
        with inf.factory() as s:
            jobs = s.scalar(select(func.count()).select_from(Job))
            keys = s.scalar(select(func.count()).select_from(DeliveryKey))
            queued = s.scalar(
                select(func.count()).select_from(Job).where(Job.status == state.QUEUED)
            )
        assert jobs == keys == queued == total
        r = redis_sync.Redis.from_url(inf.redis_url)
        try:
            assert r.zcard("arq:queue") == total  # every accepted job was handed to ARQ
        finally:
            r.close()

    timings.sort()
    stats = {
        "n": float(len(timings)),
        "p50_s": percentile(timings, 50),
        "p95_s": percentile(timings, 95),
        "p99_s": percentile(timings, 99),
        "max_s": timings[-1],
    }
    _report("webhook_latency", stats)
    assert len(timings) >= 200
    assert stats["p95_s"] < P95_LIMIT_S, stats


def test_500_line_pr_full_pipeline_within_10_minutes() -> None:
    files = bench_files()
    with (
        tempfile.TemporaryDirectory(prefix="prr-bench-") as tmp,
        infra("perf-pipe") as inf,
    ):
        root = Path(tmp)
        git_root, workspaces, logs = root / "git", root / "ws", root / "logs"
        workspaces.mkdir()
        logs.mkdir()
        prs = build_origin(git_root, {1: files})
        pr = prs[1]
        assert pr.additions + pr.deletions <= MAX_CHANGED_LINES, (pr.additions, pr.deletions)
        assert pr.additions + pr.deletions > 150  # a real PR, not a token one

        st = make_stack(inf)
        gh, llm = StubGitHub(prs), StubLlm()
        pool = WorkerPool(
            worker_env(st, llm, gh, git_root, workspaces, TEST_TIMEOUT_SECONDS="240"),
            1, logs,
        )  # fmt: skip
        try:
            pool.start()
            t0 = time.monotonic()
            with webhook_client(st) as client:
                assert deliver(client, st.secret, 1, pr.head_sha).status_code == 202
            status = ""
            while time.monotonic() - t0 < PIPELINE_LIMIT_S:
                assert pool.procs[0].poll() is None, "worker exited:\n" + pool.log_tail()
                with inf.factory() as s:
                    job = JobRepository(s).find(REPO, 1, pr.head_sha)
                    status = job.status if job else ""
                if status in state.TERMINAL:
                    break
                time.sleep(1)
            took = time.monotonic() - t0
            _report("pipeline", {"changed_lines": float(pr.additions + pr.deletions), "s": took})
            assert status == state.SUCCEEDED, f"{status} after {took:.0f}s\n{pool.log_tail()}"
            assert took < PIPELINE_LIMIT_S

            with inf.factory() as s:
                job = JobRepository(s).find(REPO, 1, pr.head_sha)
                assert job is not None
                done = {
                    a.stage
                    for a in s.scalars(select(StageAttempt).where(StageAttempt.job_id == job.id))
                    if a.status == "succeeded"
                }
                review_rows = s.scalar(
                    select(func.count())
                    .select_from(Comment)
                    .where(Comment.job_id == job.id, Comment.kind == "review")
                )
            assert done == set(state.STAGES)
            assert review_rows == 1
            posted = gh.all_reviews()
            assert len(posted) == 1 and posted[0][1]["commit_id"] == pr.head_sha
            assert llm.faults_served == 0 and len(llm.hits) > 0  # the LLM stage really ran
            assert list(workspaces.iterdir()) == []  # checkouts removed
        finally:
            pool.stop()
            gh.shutdown()
            llm.shutdown()
