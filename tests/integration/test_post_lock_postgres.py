"""Real Postgres advisory lock + real HTTP GitHub stub: concurrent posts create one review."""

import asyncio
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from pr_review.connectors.base import PullRequestRef
from pr_review.connectors.github import auth as gh_auth
from pr_review.connectors.github import client as gh_client
from pr_review.connectors.github import posting as gh_posting
from pr_review.connectors.github.auth import GitHubAppAuth
from pr_review.connectors.github.client import GitHubClient
from pr_review.connectors.github.posting import Posted, ReviewPoster, Skipped
from pr_review.parsers.chunker import ChunkPlan
from pr_review.pipeline.stages import DbReviewStore
from pr_review.review.aggregate import aggregate
from pr_review.storage.models import Comment
from pr_review.storage.repositories import JobRepository

pytestmark = pytest.mark.docker
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "load"))
from stubs import PrData, StubGitHub, private_key_pem  # noqa: E402


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    if shutil.which("docker") is None or _docker("info").returncode != 0:
        pytest.skip("Docker unavailable")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    name = f"pr-review-pg-{uuid.uuid4().hex[:8]}"
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


def test_concurrent_posts_create_exactly_one_review(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = "a" * 40
    gh = StubGitHub({5: PrData(5, "b" * 40, sha, "", 0, 0, 0)}, latency_s=0.3)
    for mod in (gh_auth, gh_client, gh_posting):
        monkeypatch.setattr(mod, "API", gh.url)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as s:
        job = JobRepository(s).create("acme/bench", 5, sha, 7)
        s.commit()
        job_id = job.id
    plan = aggregate([], ChunkPlan((), (), False, 0, ()), received=0)

    async def attempt(client: httpx.AsyncClient) -> Posted | Skipped:
        auth = GitHubAppAuth(7, SecretStr(private_key_pem()), client)
        poster = ReviewPoster(
            auth, client, GitHubClient(auth, client), store=DbReviewStore(factory, job_id)
        )
        return await poster.post_review(PullRequestRef(7, "acme/bench", 5), sha, plan, "summary")

    async def run() -> list[Posted | Skipped]:
        async with httpx.AsyncClient() as c1, httpx.AsyncClient() as c2:
            return list(await asyncio.gather(attempt(c1), attempt(c2)))

    try:
        results = asyncio.run(run())
    finally:
        gh.shutdown()
    assert sum(isinstance(r, Posted) for r in results) == 1
    assert [r.reason for r in results if isinstance(r, Skipped)] == ["duplicate"]
    assert len(gh.all_reviews()) == 1
    with factory() as s:
        assert (
            s.scalar(select(func.count()).select_from(Comment).where(Comment.kind == "review")) == 1
        )
