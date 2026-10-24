"""Stage orchestration against everything real: a PR in the private sandbox repo, GitHub App
credentials from ~/.config/pr-review-agent/env.test, a throwaway Postgres 16 container, the Docker
sandbox and a local Ollama model (PR_REVIEW_TEST_LLM_MODEL, default qwen2.5-coder:1.5b).

PR content comes from the scenario library in tests/e2e/scenarios. Skipped only when a service is
unavailable. Every PR is closed and its branch deleted; sandbox containers, networks and volumes
are removed.
"""

import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from pr_review.connectors.github.auth import API, GitHubAppAuth
from pr_review.core.settings import Settings
from pr_review.llm.budget import provider_hooks
from pr_review.llm.provider import Provider
from pr_review.llm.resilience import BreakerRegistry
from pr_review.pipeline import lease, queue, state
from pr_review.pipeline.stages import Pipeline
from pr_review.storage.models import (
    Comment,
    Event,
    Finding,
    Job,
    LlmCall,
    StageAttempt,
)
from pr_review.storage.repositories import JobRepository

pytestmark = [pytest.mark.github, pytest.mark.llm, pytest.mark.docker]

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = Path(
    os.environ.get("PR_REVIEW_GH_TEST_ENV", "~/.config/pr-review-agent/env.test")
).expanduser()
OLLAMA = os.environ.get("PR_REVIEW_TEST_OLLAMA", "http://localhost:11434")
_MODEL = os.environ.get("PR_REVIEW_TEST_LLM_MODEL", "qwen2.5-coder:1.5b")
MODEL = _MODEL if "/" in _MODEL else f"ollama/{_MODEL}"
MARKER = f"prr-src-marker-{uuid.uuid4().hex}"  # appears only in repo source, never in storage
SANDBOX_LABEL = "label=pr-review.sandbox"

sys.path.insert(0, str(ROOT / "tests" / "e2e"))
from scenarios import load, repo_files  # noqa: E402  (scenario library, shared with the e2e tests)


def good(*ids: str) -> dict[str, str]:
    """A pull request built from library scenarios; the first source file also carries MARKER
    on a trailing comment line, so no recorded line number moves."""
    files = repo_files(*ids)
    src = next(p for p in files if p.endswith(".py") and not p.split("/")[-1].startswith("test_"))
    files[src] += f"# {MARKER}\n"
    return files


def source_line(sid: str) -> str:
    """The first anchor line of a scenario's expected finding, as it appears in the source."""
    s = load(sid)
    f = s.findings[0]
    return s.files[f.path].splitlines()[f.line - 1].strip()


# one cheap model category, no sandbox work: for tests about orchestration, not analysis
CHEAP_CONFIG = "tests_disabled: true\ncategories: [correctness]\n"
ONE_CATEGORY = "categories: [correctness]\n"


def _env() -> dict[str, str]:
    if not ENV_FILE.is_file():
        return {}
    pairs = (ln.split("=", 1) for ln in ENV_FILE.read_text().splitlines() if "=" in ln)
    return {k.strip(): v.strip().strip("\"'") for k, v in pairs}


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


def gh(*args: str, stdin: str | None = None) -> Any:
    out = subprocess.run(
        ["gh", "api", *args], input=stdin, capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out) if out.strip() else None


def _unavailable() -> str:
    env = _env()
    if not env.get("GITHUB_TEST_REPO") or not env.get("GITHUB_APP_SLUG"):
        return f"real GitHub test env absent ({ENV_FILE})"
    if (
        shutil.which("gh") is None
        or subprocess.run(["gh", "auth", "status"], capture_output=True).returncode
    ):
        return "gh CLI unavailable or not authenticated"
    if shutil.which("docker") is None or _docker("info").returncode != 0:
        return "Docker unavailable"
    try:
        urllib.request.urlopen(OLLAMA + "/api/tags", timeout=3).read()
    except OSError as e:
        return f"Ollama unreachable at {OLLAMA}: {e}"
    return ""


_WHY = _unavailable()
pytestmark.append(pytest.mark.skipif(bool(_WHY), reason=_WHY or "x"))


# --- fixtures --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    name = f"pr-review-stages-{uuid.uuid4().hex[:8]}"
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
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(scope="module")
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


@pytest.fixture(scope="module")
def workspaces(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("prr-stage-ws")


@dataclass
class Env:
    repo: str
    slug: str
    installation_id: int
    pipeline: Pipeline
    factory: sessionmaker[Session]
    loop: asyncio.AbstractEventLoop


@pytest.fixture(scope="module")
def env(
    factory: sessionmaker[Session],
    loop: asyncio.AbstractEventLoop,
    workspaces: Path,
    pkg_index: str | None,
) -> Iterator[Env]:
    e = _env()
    key = Path(e["GITHUB_PRIVATE_KEY_PATH"]).expanduser().read_text()
    settings = Settings(
        github_app_id=int(e["GITHUB_APP_ID"]),
        github_private_key=SecretStr(key),
        github_webhook_secret=SecretStr("unused"),
        database_url=SecretStr("postgresql://unused"),
        redis_url=SecretStr("redis://unused"),
        llm_review_model=MODEL,
        llm_triage_model=MODEL,
        ollama_api_base=OLLAMA,
        workspace_root=str(workspaces),
        test_timeout_seconds=240.0,
        sandbox_pip_index_url=pkg_index,
    )
    http = httpx.AsyncClient()
    provider = Provider(settings, breakers=BreakerRegistry(), **provider_hooks(factory))
    auth = GitHubAppAuth(settings.github_app_id, settings.github_private_key, http)
    inst = httpx.get(
        f"{API}/repos/{e['GITHUB_TEST_REPO']}/installation",
        headers={"Authorization": f"Bearer {auth.app_jwt()}"},
    ).json()["id"]
    try:
        yield Env(
            e["GITHUB_TEST_REPO"],
            e["GITHUB_APP_SLUG"],
            int(inst),
            Pipeline(settings, factory, provider, http),
            factory,
            loop,
        )
    finally:
        loop.run_until_complete(http.aclose())
        # nothing the sandbox created may outlive the module
        for kind, ls in (("container", "ps -aq"), ("network", "network ls -q"),
                         ("volume", "volume ls -q")):  # fmt: skip
            ids = _docker(*ls.split(), "--filter", SANDBOX_LABEL).stdout.split()
            if ids:
                rm = ["rm", "-fv"] if kind == "container" else [kind, "rm"]
                _docker(*rm, *ids)


@dataclass
class Pr:
    number: int
    branch: str
    head_sha: str


def _put(repo: str, branch: str, path: str, content: str, message: str) -> str:
    body: dict[str, str] = {
        "message": message,
        "branch": branch,
        "content": base64.b64encode(content.encode()).decode(),
    }
    try:
        body["sha"] = gh(f"repos/{repo}/contents/{path}?ref={branch}")["sha"]
    except subprocess.CalledProcessError:
        pass  # new file
    for attempt in range(4):  # GitHub answers 409/5xx to quick consecutive commits on a branch
        try:
            r = gh("-X", "PUT", f"repos/{repo}/contents/{path}", "--input", "-",
                   stdin=json.dumps(body))  # fmt: skip
            return str(r["commit"]["sha"])
        except subprocess.CalledProcessError:
            if attempt == 3:
                raise
            time.sleep(2)
    raise AssertionError("unreachable")


def _head(repo: str, number: int) -> str:
    return str(gh(f"repos/{repo}/pulls/{number}")["head"]["sha"])


def create_pr(repo: str, files: dict[str, str], made: list[Pr]) -> Pr:
    """Open a PR adding `files`; it is appended to `made` as soon as its branches exist."""
    branch = f"prr-e2e-{uuid.uuid4().hex[:8]}"
    # Both branches start at the repo's first commit, and the PR targets a throwaway base:
    # whatever else lands on main can neither hide our files from the diff nor be merged into.
    root = subprocess.run(
        f"gh api --paginate repos/{repo}/commits --jq '.[].sha' | tail -1",
        shell=True, capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    base = f"{branch}-base"
    for name in (base, branch):
        gh("-X", "POST", f"repos/{repo}/git/refs", "-f", f"ref=refs/heads/{name}",
           "-f", f"sha={root}")  # fmt: skip
    pr = Pr(0, branch, "")
    made.append(pr)
    for path, content in files.items():
        _put(repo, branch, path, content, f"add {path}")
    data = gh(
        "-X", "POST", f"repos/{repo}/pulls", "-f", f"head={branch}", "-f", f"base={base}",
        "-f", f"title=e2e {branch}", "-f", "body=automated",
    )  # fmt: skip
    pr.number = data["number"]
    pr.head_sha = _head(repo, pr.number)
    return pr


def close_prs(repo: str, made: list[Pr]) -> None:
    """Always close the PRs and delete their branches."""
    for pr in made:
        if pr.number:
            subprocess.run(["gh", "api", "-X", "PATCH", f"repos/{repo}/pulls/{pr.number}",
                            "-f", "state=closed"], capture_output=True)  # fmt: skip
        for name in (pr.branch, f"{pr.branch}-base"):
            ref = f"repos/{repo}/git/refs/heads/{name}"
            subprocess.run(["gh", "api", "-X", "DELETE", ref], capture_output=True)


@pytest.fixture
def open_pr(env: Env) -> Iterator[Any]:
    made: list[Pr] = []
    yield lambda files: create_pr(env.repo, files, made)
    close_prs(env.repo, made)


# --- helpers ---------------------------------------------------------------------------------


def new_job(env: Env, pr: Pr) -> uuid.UUID:
    with env.factory() as s:
        job = JobRepository(s).create(env.repo, pr.number, pr.head_sha, env.installation_id)
        s.commit()
        return job.id


def drive(env: Env, job_id: uuid.UUID, handlers: dict[str, queue.Stage] | None = None) -> str:
    """Run the real worker loop; a retryable failure is picked up again like the sweeper would."""
    handlers = handlers or env.pipeline.handlers()
    out = "queued"
    for _ in range(6):
        out = env.loop.run_until_complete(
            queue.run_job(
                env.factory, handlers, job_id, "stage-test", on_failure=env.pipeline.post_failure
            )
        )
        if out != "queued":
            break
    return out


def bot_reviews(env: Env, pr: Pr) -> list[dict[str, Any]]:
    revs = list(gh("--paginate", f"repos/{env.repo}/pulls/{pr.number}/reviews"))
    return [r for r in revs if r["user"]["login"] == f"{env.slug}[bot]"]


def inline_comments(env: Env, pr: Pr) -> list[dict[str, Any]]:
    return list(gh("--paginate", f"repos/{env.repo}/pulls/{pr.number}/comments"))


def attempts(env: Env, job_id: uuid.UUID) -> dict[str, list[str]]:
    with env.factory() as s:
        rows = s.scalars(
            select(StageAttempt).where(StageAttempt.job_id == job_id).order_by(StageAttempt.id)
        )
        out: dict[str, list[str]] = {}
        for a in rows:
            out.setdefault(a.stage, []).append(a.status)
        return out


def status(env: Env, job_id: uuid.UUID) -> str:
    with env.factory() as s:
        return str(s.scalars(select(Job.status).where(Job.id == job_id)).one())


def assert_no_source_in_db(env: Env, job_id: uuid.UUID, source: str) -> None:
    """Every stored row for the job, as text: no source marker, diff, prompt or raw artifact."""
    with env.factory() as s:
        dump = ""
        for table in ("events", "findings", "llm_calls", "test_runs", "comments",
                      "stage_attempts", "jobs"):  # fmt: skip
            key = "id" if table == "jobs" else "job_id"
            dump += str(
                s.execute(
                    text(f"select t::text from {table} t where {key} = :j"), {"j": job_id}
                ).all()
            )
    for needle in (MARKER, "diff --git", "@@ -", "<<<UNTRUSTED", source, "<testsuite"):
        assert needle not in dump, f"{needle!r} leaked into the database"


def assert_clean_up(env: Env, workspaces: Path) -> None:
    assert list(workspaces.iterdir()) == []  # every checkout removed
    for ls in ("ps -aq", "network ls -q", "volume ls -q"):
        assert _docker(*ls.split(), "--filter", SANDBOX_LABEL).stdout.split() == []


# --- scenarios -------------------------------------------------------------------------------


def test_full_run_posts_one_review_with_inline_comment(
    env: Env, open_pr: Any, workspaces: Path
) -> None:
    pr = open_pr(good("off_by_one", "sql_injection"))
    job_id = new_job(env, pr)

    assert drive(env, job_id) == "succeeded"

    assert attempts(env, job_id) == {s: ["succeeded"] for s in state.STAGES}
    reviews = bot_reviews(env, pr)
    assert len(reviews) == 1 and reviews[0]["state"] == "COMMENTED"
    assert reviews[0]["commit_id"] == pr.head_sha
    comments = [
        c for c in inline_comments(env, pr) if c["pull_request_review_id"] == reviews[0]["id"]
    ]
    assert len(comments) >= 1, reviews[0]["body"][:2000]
    body = reviews[0]["body"]
    assert "1 failed" in body  # the failing test is reported
    assert "test_first_page_has_exactly_size_items" in body or any(
        "test-" in c["body"] for c in comments
    )
    print(f"review {reviews[0]['id']}: {len(comments)} inline; model {MODEL}")
    for c in comments:
        print("  inline:", c["path"], c["line"], c["body"].splitlines()[0][:120])

    with env.factory() as s:
        findings = list(s.scalars(select(Finding).where(Finding.job_id == job_id)))
        calls = list(s.scalars(select(LlmCall).where(LlmCall.job_id == job_id)))
        stored = list(s.scalars(select(Comment).where(Comment.job_id == job_id)))
    assert findings and any(f.agent_type == "static" for f in findings)
    assert calls and all(c.model == MODEL and c.outcome for c in calls)
    assert {c.github_comment_id for c in stored if c.kind == "review"} == {reviews[0]["id"]}
    assert len([c for c in stored if c.kind == "inline"]) == len(comments)
    assert_no_source_in_db(env, job_id, source_line("sql_injection"))

    # a second delivery of the same job (and a replayed post) adds nothing
    assert drive(env, job_id) == "skipped"
    assert len(bot_reviews(env, pr)) == 1
    assert_clean_up(env, workspaces)


def test_failed_test_stage_still_posts_summary_naming_the_stage(
    env: Env, open_pr: Any, workspaces: Path
) -> None:
    bad = {
        **good("none_dereference"),
        "requirements.txt": "--index-url https://example.com/simple\npytest\n",
    }
    bad[".pr-review.yml"] = ONE_CATEGORY
    pr = open_pr(bad)
    job_id = new_job(env, pr)

    assert drive(env, job_id) == "dead_letter"

    assert attempts(env, job_id)["test"] == ["failed"]  # permanent: no second attempt
    assert "parse" not in attempts(env, job_id)
    reviews = bot_reviews(env, pr)
    assert len(reviews) == 1
    assert (
        "stage 'test' failed" in reviews[0]["body"]
        and "analysis was incomplete" in reviews[0]["body"]
    )
    assert inline_comments(env, pr) == []  # summary-only
    with env.factory() as s:
        job = s.get(Job, job_id)
        assert job is not None and "test" in (job.dead_letter_reason or "")
    assert_no_source_in_db(env, job_id, source_line("none_dereference"))
    assert_clean_up(env, workspaces)


def test_crash_after_test_stage_resumes_without_rerunning_stages(
    env: Env, open_pr: Any, workspaces: Path
) -> None:
    pr = open_pr({**good("clean_change"), ".pr-review.yml": ONE_CATEGORY})
    job_id = new_job(env, pr)
    handlers = env.pipeline.handlers()

    async def crash(_: uuid.UUID) -> None:  # the worker process dies as `parse` begins
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        drive(env, job_id, {**handlers, "parse": crash})
    assert status(env, job_id) == "running"
    assert attempts(env, job_id) == {
        "fetch": ["succeeded"],
        "detect": ["succeeded"],
        "test": ["succeeded"],
        "parse": ["running"],
    }

    # the lease expires; the sweeper's recovery re-leases the job and queues the retry
    past = datetime.now(UTC) - timedelta(minutes=1)
    with env.factory() as s:
        s.execute(update(Job).where(Job.id == job_id).values(lease_expires_at=past))
        s.commit()
    sent: list[uuid.UUID] = []

    async def enqueue(j: uuid.UUID, _: float) -> None:
        sent.append(j)

    assert env.loop.run_until_complete(
        lease.recover_expired(env.factory, enqueue, queue.CFG, "sweeper")
    ) == 1 and sent == [job_id]

    assert drive(env, job_id) == "succeeded"
    assert attempts(env, job_id) == {
        "fetch": ["succeeded"],
        "detect": ["succeeded"],
        "test": ["succeeded"],  # not re-run
        "parse": ["failed", "succeeded"],
        "analyse": ["succeeded"],
        "aggregate": ["succeeded"],
        "post": ["succeeded"],
    }
    assert len(bot_reviews(env, pr)) == 1
    assert_clean_up(env, workspaces)


def test_stale_head_posts_nothing(env: Env, open_pr: Any, workspaces: Path) -> None:
    pr = open_pr({**good("quadratic_loop"), ".pr-review.yml": CHEAP_CONFIG})
    job_id = new_job(env, pr)
    handlers = env.pipeline.handlers()
    real_post = handlers["post"]

    async def push_then_post(j: uuid.UUID) -> None:
        # a new commit lands after analysis, before the review is posted
        _put(env.repo, pr.branch, "dedupe.py", good("quadratic_loop")["dedupe.py"] + "# newer\n",
             "newer commit")  # fmt: skip
        for _ in range(60):
            if _head(env.repo, pr.number) != pr.head_sha:
                break
            time.sleep(1)
        else:
            raise AssertionError("PR head never advanced")
        await real_post(j)

    assert drive(env, job_id, {**handlers, "post": push_then_post}) == "succeeded"
    assert bot_reviews(env, pr) == [] and inline_comments(env, pr) == []
    with env.factory() as s:
        posted = s.scalars(
            select(Event.payload).where(Event.job_id == job_id, Event.stage == "post",
                                        Event.type == "stage_output")
        ).one()  # fmt: skip
        assert posted["data"]["outcome"] == "stale"
        assert s.scalars(select(Comment).where(Comment.job_id == job_id)).first() is None
    assert_clean_up(env, workspaces)
