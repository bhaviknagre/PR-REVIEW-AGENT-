"""Real end to end: a signed GitHub webhook to a review posted on the sandbox repository.

Everything is real. The sandbox repo (dev GitHub App from ~/.config/pr-review-agent/env.test)
gets branches and pull requests through the `gh` CLI. A signed `pull_request` webhook is POSTed to
`create_app()`; a separate `arq` worker process picks the job up from a Redis container and runs
fetch, detect, sandbox tests, parse, LLM analysis (local Ollama), aggregate and post against a
Postgres container and Docker sandbox. The assertions read the result back from the GitHub API.

Two sandbox PRs per run: one carrying a representative 6-scenario buggy subset (the rest are
covered offline by test_scenarios.py; all 13 at once is too slow for a local 7B model), and one
clean change. Both are closed and their branches deleted at the end, containers are removed
with their volumes.

LLM tolerance: deterministic findings (secret scan, injection-sink patterns) must always be posted
on the exact file and line. Model-only scenarios (`detector == "llm"`) are checked against
MIN_LLM_HITS, which is 2 of 5 for a review model of 7B parameters or more and 0 for the small CI
models (qwen2.5-coder:0.5b/1.5b), which cannot be expected to find real bugs reliably; for those
the test still requires that LLM calls ran and succeeded. The clean PR may attract a little model
noise (at most MAX_CLEAN_INLINE comments, none CRITICAL) but never a deterministic finding.
"""

# ruff: noqa: E402

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "integration")]

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr
from scenarios import Expected, expected, repo_files
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from test_stages import (
    _WHY,
    OLLAMA,
    SANDBOX_LABEL,
    Pr,
    _docker,
    _env,
    close_prs,
    create_pr,
    gh,
)

from pr_review.api.app import create_app
from pr_review.connectors.github.auth import API, GitHubAppAuth
from pr_review.core.settings import Settings
from pr_review.pipeline import state
from pr_review.storage.models import Comment, Finding, Job, LlmCall, StageAttempt
from pr_review.storage.repositories import JobRepository

pytestmark = [pytest.mark.e2e, pytest.mark.github, pytest.mark.llm, pytest.mark.docker]
pytestmark.append(pytest.mark.skipif(bool(_WHY), reason=_WHY or "x"))

ROOT = HERE.parents[1]
_OVERRIDE = os.environ.get("PR_REVIEW_TEST_LLM_MODEL")
_prefixed = (lambda m: m if "/" in m else f"ollama/{m}") if _OVERRIDE else None
REVIEW_MODEL = _prefixed(_OVERRIDE) if _prefixed and _OVERRIDE else "ollama/qwen2.5-coder:7b"
TRIAGE_MODEL = _prefixed(_OVERRIDE) if _prefixed and _OVERRIDE else "ollama/qwen2.5-coder:1.5b"
_SIZE = re.search(r":(\d+(?:\.\d+)?)b", REVIEW_MODEL)
MIN_LLM_HITS = 2 if _SIZE and float(_SIZE.group(1)) >= 7 else 0
LINE_SLACK = 3  # an LLM finding counts when it lands within this many lines of the bug
_BIG = bool(_SIZE and float(_SIZE.group(1)) >= 7)
# Small models are noisy (known since T-39): the comment budget only binds the 7B production model;
# every model must still post no CRITICAL and no deterministic finding on the clean PR.
MAX_CLEAN_INLINE = 3 if _BIG else 25
TIMEOUT_S = float(os.environ.get("PR_REVIEW_E2E_TIMEOUT", "1800"))

BUGGY = [
    "sql_injection", "hardcoded_secret", "unsafe_deserialization", "off_by_one",
    "none_dereference", "mutable_default",
]  # fmt: skip
CLEAN = ["clean_change"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class Stack:
    repo: str
    slug: str
    installation_id: int
    secret: str
    client: TestClient
    factory: sessionmaker[Session]
    engine: Engine
    workspaces: Path
    worker: "subprocess.Popen[bytes]"
    worker_log: Path


def _run_container(name: str, image: str, port: int, inner: int, *args: str) -> None:
    run = _docker(
        "run", "-d", "--name", name, *args, "-p", f"127.0.0.1:{port}:{inner}", image
    )  # no --rm: `rm -fv` below removes the anonymous volume with the container
    if run.returncode != 0:
        pytest.skip(f"cannot start {image}: {run.stderr.strip()}")


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    e = _env()
    suffix = uuid.uuid4().hex[:8]
    pg_name, redis_name = f"pr-review-e2e-pg-{suffix}", f"pr-review-e2e-redis-{suffix}"
    pg_port, redis_port = _free_port(), _free_port()
    workspaces = tmp_path_factory.mktemp("prr-e2e-ws")
    worker: subprocess.Popen[bytes] | None = None
    log_path = tmp_path_factory.mktemp("prr-e2e-log") / "worker.log"
    try:
        _run_container(pg_name, "postgres:16-alpine", pg_port, 5432, "-e", "POSTGRES_PASSWORD=pw")
        _run_container(redis_name, "redis:7.4-alpine", redis_port, 6379)
        db_url = f"postgresql+psycopg://postgres:pw@127.0.0.1:{pg_port}/postgres"
        redis_url = f"redis://127.0.0.1:{redis_port}"
        engine = create_engine(db_url, pool_pre_ping=True)
        for _ in range(60):
            try:
                engine.connect().close()
                break
            except Exception:
                time.sleep(1)
        else:
            pytest.fail("postgres did not become ready")
        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "src/pr_review/storage/migrations"))
        with engine.connect() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, "head")

        webhook_secret = secrets.token_hex(16)
        key_path = Path(e["GITHUB_PRIVATE_KEY_PATH"]).expanduser()
        worker_env = {
            **{k: v for k, v in os.environ.items() if not k.startswith(("GITHUB_", "LLM_"))},
            "DATABASE_URL": db_url,
            "REDIS_URL": redis_url,
            "GITHUB_APP_ID": e["GITHUB_APP_ID"],
            "GITHUB_PRIVATE_KEY_PATH": str(key_path),
            "GITHUB_WEBHOOK_SECRET": webhook_secret,
            "LLM_REVIEW_MODEL": REVIEW_MODEL,
            "LLM_TRIAGE_MODEL": TRIAGE_MODEL,
            "OLLAMA_API_BASE": OLLAMA,
            "WORKSPACE_ROOT": str(workspaces),
            "TEST_TIMEOUT_SECONDS": "240",
        }
        with log_path.open("wb") as log:
            worker = subprocess.Popen(
                [sys.executable, "-m", "arq", "pr_review.pipeline.queue.WorkerSettings"],
                env=worker_env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT,
            )  # fmt: skip

        settings = Settings(
            github_app_id=int(e["GITHUB_APP_ID"]),
            github_private_key=SecretStr(key_path.read_text()),
            github_webhook_secret=SecretStr(webhook_secret),
            database_url=SecretStr(db_url),
            redis_url=SecretStr(redis_url),
        )
        auth = GitHubAppAuth(
            settings.github_app_id, settings.github_private_key, httpx.AsyncClient()
        )
        inst = httpx.get(
            f"{API}/repos/{e['GITHUB_TEST_REPO']}/installation",
            headers={"Authorization": f"Bearer {auth.app_jwt()}"},
        ).json()["id"]

        os.environ["REDIS_URL"] = redis_url  # create_enqueuer reads it when the app starts
        factory = sessionmaker(engine, expire_on_commit=False)
        try:
            with TestClient(create_app(settings, engine=engine)) as client:
                yield Stack(
                    e["GITHUB_TEST_REPO"], e["GITHUB_APP_SLUG"], int(inst), webhook_secret,
                    client, factory, engine, workspaces, worker, log_path,
                )  # fmt: skip
        finally:
            os.environ.pop("REDIS_URL", None)
        engine.dispose()
    finally:
        if worker is not None:
            worker.terminate()
            try:
                worker.wait(timeout=30)
            except subprocess.TimeoutExpired:
                worker.kill()
        _docker("rm", "-fv", pg_name, redis_name)
        for kind, ls in (("rm", "ps -aq"), ("network rm", "network ls -q"),
                         ("volume rm", "volume ls -q")):  # fmt: skip
            ids = _docker(*ls.split(), "--filter", SANDBOX_LABEL).stdout.split()
            if ids:
                _docker(*kind.split(), *(["-fv"] if kind == "rm" else []), *ids)


@pytest.fixture(scope="module")
def made() -> Iterator[list[Pr]]:
    prs: list[Pr] = []
    yield prs
    close_prs(_env()["GITHUB_TEST_REPO"], prs)


# --- helpers ---------------------------------------------------------------------------------


def deliver(
    s: Stack, pr: Pr, *, delivery: str | None = None, action: str = "opened"
) -> httpx.Response:
    """POST a correctly HMAC-signed pull_request webhook the way GitHub sends it."""
    import hashlib
    import hmac

    payload = {
        "action": action,
        "number": pr.number,
        "pull_request": {"number": pr.number, "head": {"sha": pr.head_sha}},
        "repository": {"full_name": s.repo},
        "installation": {"id": s.installation_id},
    }
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(s.secret.encode(), body, hashlib.sha256).hexdigest()
    return s.client.post(
        "/webhook/github",
        content=body,
        headers={
            "content-type": "application/json",
            "x-github-event": "pull_request",
            "x-github-delivery": delivery or str(uuid.uuid4()),
            "x-hub-signature-256": sig,
        },
    )


def wait_for_job(s: Stack, pr: Pr) -> uuid.UUID:
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        assert s.worker.poll() is None, "worker exited:\n" + s.worker_log.read_text()[-3000:]
        with s.factory() as session:
            job = JobRepository(session).find(s.repo, pr.number, pr.head_sha)
            if job is not None and job.status in state.TERMINAL:
                return job.id
        time.sleep(3)
    raise AssertionError(f"job not finished in {TIMEOUT_S}s:\n" + s.worker_log.read_text()[-3000:])


def bot_reviews(s: Stack, pr: Pr) -> list[dict[str, Any]]:
    revs = list(gh("--paginate", f"repos/{s.repo}/pulls/{pr.number}/reviews"))
    return [r for r in revs if r["user"]["login"] == f"{s.slug}[bot]"]


def review_comments(s: Stack, pr: Pr, review_id: int) -> list[dict[str, Any]]:
    cs = list(gh("--paginate", f"repos/{s.repo}/pulls/{pr.number}/comments"))
    return [c for c in cs if c["pull_request_review_id"] == review_id]


def hit(comments: list[dict[str, Any]], e: Expected, slack: int) -> bool:
    return any(c["path"] == e.path and abs(int(c["line"]) - e.line) <= slack for c in comments)


def assert_pipeline_ran(s: Stack, job_id: uuid.UUID) -> None:
    with s.factory() as session:
        job = session.get(Job, job_id)
        assert job is not None and job.status == state.SUCCEEDED, job and job.dead_letter_reason
        stages = {
            (a.stage, a.status)
            for a in session.scalars(select(StageAttempt).where(StageAttempt.job_id == job_id))
        }
        calls = list(session.scalars(select(LlmCall).where(LlmCall.job_id == job_id)))
    assert stages == {(st, "succeeded") for st in state.STAGES}
    assert calls and any(c.outcome == "ok" for c in calls) and {c.model for c in calls} <= {
        REVIEW_MODEL, TRIAGE_MODEL,
    }  # fmt: skip


def assert_redelivery_is_idempotent(s: Stack, pr: Pr, job_id: uuid.UUID, review_id: int) -> None:
    same_delivery = str(uuid.uuid4())
    first = deliver(s, pr, delivery=same_delivery, action="synchronize")
    again = deliver(s, pr, delivery=same_delivery, action="synchronize")  # exact redelivery
    fresh = deliver(s, pr, action="reopened")  # new delivery id, same head sha
    assert (first.status_code, again.status_code, fresh.status_code) == (202, 202, 202)
    assert (first.text, again.text, fresh.text) == ("duplicate",) * 3
    time.sleep(10)  # a wrongly accepted job would have been picked up by now
    with s.factory() as session:
        jobs = session.scalar(
            select(func.count())
            .select_from(Job)
            .where(Job.repo == s.repo, Job.pr_number == pr.number)
        )
        reviews = session.scalar(
            select(func.count())
            .select_from(Comment)
            .where(Comment.job_id == job_id, Comment.kind == "review")
        )
    assert (jobs, reviews) == (1, 1)
    assert [r["id"] for r in bot_reviews(s, pr)] == [review_id]


def test_unsigned_webhook_is_rejected(stack: Stack) -> None:
    r = stack.client.post(
        "/webhook/github", content=b"{}",
        headers={"x-github-event": "pull_request", "x-hub-signature-256": "sha256=" + "0" * 64},
    )  # fmt: skip
    assert r.status_code == 401


# --- scenarios -------------------------------------------------------------------------------


def test_buggy_pr_webhook_to_review(stack: Stack, made: list[Pr]) -> None:
    t0 = time.monotonic()
    pr = create_pr(stack.repo, repo_files(*BUGGY), made)
    resp = deliver(stack, pr)
    assert resp.status_code == 202 and resp.text != "duplicate"
    job_id = wait_for_job(stack, pr)
    took = time.monotonic() - t0
    assert_pipeline_ran(stack, job_id)

    reviews = bot_reviews(stack, pr)
    assert len(reviews) == 1, "exactly one review"
    review = reviews[0]
    assert review["commit_id"] == pr.head_sha and review["state"] == "COMMENTED"
    assert "PR Review Agent" in review["body"]
    assert "2 failed" in review["body"]  # off_by_one and none_dereference ship a failing test
    comments = review_comments(stack, pr, review["id"])

    # deterministic findings: always, on the exact file and line
    static = [e for e in expected(*BUGGY) if e.detector == "static"]
    missing = [(e.path, e.line) for e in static if not hit(comments, e, 0)]
    assert missing == [], f"deterministic findings not posted inline: {missing}"
    with stack.factory() as session:
        stored = list(session.scalars(select(Finding).where(Finding.job_id == job_id)))
    assert sum(f.agent_type == "static" for f in stored) >= len(static)
    assert any(f.severity == "CRITICAL" and f.file_path == "billing_client.py" for f in stored)

    # model-dependent findings: tolerance documented in the module docstring
    llm = [e for e in expected(*BUGGY) if e.detector == "llm"]
    found = [e for e in llm if hit(comments, e, LINE_SLACK)]
    print(f"model {REVIEW_MODEL}; {took:.0f}s; {len(comments)} inline comments")
    print(f"LLM scenarios found {len(found)}/{len(llm)}: {[e.path for e in found]}")
    lint = [e for e in expected(*BUGGY) if e.detector == "lint"]
    print(f"lint findings posted: {[e.path for e in lint if hit(comments, e, 0)]}")
    assert len(found) >= MIN_LLM_HITS, f"LLM found {[e.path for e in found]} of {len(llm)}"

    # every inline comment sits on a file the PR added
    assert {c["path"] for c in comments} <= set(repo_files(*BUGGY))

    assert_redelivery_is_idempotent(stack, pr, job_id, review["id"])
    assert list(stack.workspaces.iterdir()) == []  # checkout removed
    for ls in ("ps -aq", "network ls -q", "volume ls -q"):
        assert _docker(*ls.split(), "--filter", SANDBOX_LABEL).stdout.split() == []


def test_clean_pr_is_not_flooded(stack: Stack, made: list[Pr]) -> None:
    pr = create_pr(stack.repo, repo_files(*CLEAN), made)
    assert deliver(stack, pr).status_code == 202
    job_id = wait_for_job(stack, pr)
    assert_pipeline_ran(stack, job_id)

    reviews = bot_reviews(stack, pr)
    assert len(reviews) == 1 and "PR Review Agent" in reviews[0]["body"]
    assert "0 failed" in reviews[0]["body"]
    comments = review_comments(stack, pr, reviews[0]["id"])
    with stack.factory() as session:
        stored = list(session.scalars(select(Finding).where(Finding.job_id == job_id)))
    print(
        f"clean PR: {len(comments)} inline, findings {[(f.severity, f.agent_type) for f in stored]}"
    )
    assert len(comments) <= MAX_CLEAN_INLINE
    assert not any(f.agent_type == "static" for f in stored)
    assert not any(f.severity == "CRITICAL" for f in stored)

    assert_redelivery_is_idempotent(stack, pr, job_id, reviews[0]["id"])
    assert list(stack.workspaces.iterdir()) == []
