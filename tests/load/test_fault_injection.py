"""T-46 fault injection (NFR-7, NFR-9, NFR-11, AC-26): 50 queued jobs, three real `arq` worker
subprocesses, real Postgres and Redis containers, and faults injected while jobs are in flight:

1. SIGKILL of two workers mid-stage (respawned at once, like a process supervisor would);
2. `docker restart` of the Redis container;
3. LLM outage window: 500, then 429, then connections held past the call (stub LLM server);
4. GitHub outage window: every request answered 502, plus three review creations that are
   committed and then answered 502 (response lost), the nastiest case for exactly-once posting.

Faults fire on progress (terminal job count) so they land mid-run whatever the machine's speed.
The end state is read from Postgres and from the GitHub stub, which keeps every review created:
no lost job, every job terminal before the deadline, each succeeded job has exactly one review
(and one set of unique inline comments) on its own head SHA, each dead-lettered job has a
non-empty reason and at most the one failure-summary review. Total runtime is bounded; containers
(`rm -fv`), workers and temp dirs are removed in all cases.
"""

# ruff: noqa: E402

import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "e2e")]

from scenarios import all_ids, repo_files
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from stubs import (
    CHEAP_CONFIG,
    Infra,
    PrData,
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
from pr_review.storage.models import Comment, Job

pytestmark = [pytest.mark.load, pytest.mark.docker]

JOBS = 50
WORKERS = 3
RUN_BUDGET_S = 420.0  # all jobs must be terminal by then
TOTAL_BUDGET_S = 600.0  # whole test, including container start and teardown
JOB_DEADLINE_S = 400  # worker-side overall job deadline; later work dead-letters, never hangs
MIN_SUCCEEDED = 30  # sanity floor: faults delay and sometimes dead-letter, they must not gut it
LOST_REVIEW_RESPONSES = 3


def pr_files(n: int, ids: list[str]) -> dict[str, str]:
    """A small, distinct PR per job: one library scenario plus a per-PR config comment."""
    files = repo_files(ids[n % len(ids)], requirements=False)
    files[".pr-review.yml"] = CHEAP_CONFIG + f"# pr {n}\n"
    return files


def counts(factory: sessionmaker[Session]) -> Counter[str]:
    with factory() as s:
        return Counter(s.scalars(select(Job.status)))


def test_fifty_jobs_survive_kills_redis_restart_and_llm_github_outages() -> None:
    t_start = time.monotonic()
    ids = all_ids()
    with (
        tempfile.TemporaryDirectory(prefix="prr-load-") as tmp,
        infra("load") as inf,
    ):
        root = Path(tmp)
        git_root, workspaces, logs = root / "git", root / "ws", root / "logs"
        workspaces.mkdir()
        logs.mkdir()
        prs = build_origin(git_root, {n: pr_files(n, ids) for n in range(1, JOBS + 1)})
        st = make_stack(inf)
        # Slow stubs so jobs are in flight long enough for faults to hit them mid-stage.
        gh, llm = StubGitHub(prs, latency_s=0.15), StubLlm(delay_s=2.5)
        gh.lose_next_reviews = LOST_REVIEW_RESPONSES
        pool = WorkerPool(
            worker_env(
                st, llm, gh, git_root, workspaces,
                WORKER_MAX_JOBS="2", WORKER_LEASE_SECONDS="20", SWEEP_INTERVAL_SECONDS="5",
                JOB_DEADLINE_SECONDS=str(JOB_DEADLINE_S),
            ),
            WORKERS, logs,
        )  # fmt: skip
        try:
            with webhook_client(st) as client:
                limiter = client.app.state.limiter  # type: ignore[attr-defined]
                limiter._ip_limit = limiter._installation_limit = 10**9
                for n, pr in prs.items():
                    assert deliver(client, st.secret, n, pr.head_sha).status_code == 202
            with inf.factory() as s:
                assert s.scalar(select(func.count()).select_from(Job)) == JOBS
            pool.start()
            _chaos(inf, pool, gh, llm)
            _assert_end_state(inf, gh, llm, pool, prs)
        finally:
            pool.stop()
            gh.shutdown()
            llm.shutdown()
            gh.server_close()
            llm.server_close()
    assert time.monotonic() - t_start < TOTAL_BUDGET_S


def _chaos(inf: Infra, pool: WorkerPool, gh: StubGitHub, llm: StubLlm) -> None:
    """Drive the workload to completion while firing each fault once, on progress."""
    t0 = time.monotonic()
    fired: set[str] = set()
    redis_restarts = 0

    def fire(name: str, done: int, running: int, at_done: int, min_running: int = 0) -> bool:
        return name not in fired and done >= at_done and running >= min_running

    while time.monotonic() - t0 < RUN_BUDGET_S:
        pool.ensure()
        c = counts(inf.factory)
        done = sum(c[x] for x in state.TERMINAL)
        if done == JOBS:
            return
        running = c[state.RUNNING]
        if fire("kill-1", done, running, 0, min_running=4):
            pool.sigkill(0)  # jobs of this worker are mid-stage; their leases must expire
            pool.sigkill(1)
            fired.add("kill-1")
        elif fire("redis", done, running, 8):
            inf.restart_redis()
            redis_restarts += 1
            fired.add("redis")
        elif fire("llm", done, running, 14):
            llm.window(("500", 8.0), ("429", 6.0), ("hang", 6.0))
            fired.add("llm")
        elif fire("kill-2", done, running, 22, min_running=2):
            pool.sigkill(2)
            fired.add("kill-2")
        elif fire("github", done, running, 28):
            gh.outage(15.0)
            fired.add("github")
        time.sleep(0.5)
    # not finished: fall through, the end-state assertions name what is stuck
    assert redis_restarts <= 1


def _assert_end_state(
    inf: Infra, gh: StubGitHub, llm: StubLlm, pool: WorkerPool, prs: dict[int, PrData]
) -> None:
    with inf.factory() as s:
        jobs = list(s.scalars(select(Job)))
        review_rows = Counter(
            s.scalars(select(Comment.job_id).where(Comment.kind == "review"))
        )  # job id -> recorded reviews
    stuck = [(j.pr_number, j.status, j.leased_by) for j in jobs if j.status not in state.TERMINAL]
    assert len(jobs) == JOBS, "a job row was lost"
    assert {j.pr_number for j in jobs} == set(prs)
    assert stuck == [], f"non-terminal past the deadline: {stuck}\n{pool.log_tail()}"

    # the faults really happened
    assert pool.killed >= 3, "workers were not killed"
    assert llm.faults_served > 0, "LLM outage window never hit a request"
    assert gh.faults_served >= LOST_REVIEW_RESPONSES, "GitHub outage / lost responses not hit"

    by_pr: dict[int, list[dict[str, Any]]] = {}
    for number, review in gh.all_reviews():
        by_pr.setdefault(number, []).append(review)
    statuses = Counter(j.status for j in jobs)
    print(f"\n[load] {dict(statuses)}; worker spawns={pool.spawned}; kills={pool.killed}")

    for job in jobs:
        reviews = by_pr.get(job.pr_number, [])
        assert len(reviews) <= 1, f"PR {job.pr_number}: duplicate reviews " + repr(
            [(r["id"], r["commit_id"][:8], r["event"], r["body"][-90:]) for r in reviews]
        )
        for r in reviews:
            assert r["commit_id"] == job.head_sha
            places = [(c["path"], c["line"]) for c in r["comments"]]
            assert len(places) == len(set(places)), "duplicate inline comments"
        if job.status == state.SUCCEEDED:
            assert len(reviews) == 1, f"PR {job.pr_number}: succeeded without exactly one review"
            assert review_rows[job.id] == 1, "DB review record missing or duplicated"
        elif job.status == state.DEAD_LETTER:
            assert job.dead_letter_reason and job.dead_letter_reason.strip(), "no reason recorded"
        else:  # superseded/cancelled cannot happen here: one head per PR
            pytest.fail(f"PR {job.pr_number}: unexpected terminal status {job.status}")
    assert statuses[state.SUCCEEDED] >= MIN_SUCCEEDED, dict(statuses)
