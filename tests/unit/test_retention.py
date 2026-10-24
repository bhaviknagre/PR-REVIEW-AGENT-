"""Retention against a real Postgres 16 container and real workspace dirs on disk."""

import os
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from pr_review.core.settings import Settings
from pr_review.storage.events import AppendOnlyViolation
from pr_review.storage.models import (
    Comment,
    DeliveryKey,
    Event,
    Finding,
    Job,
    LlmCall,
    StageAttempt,
    TestRun,
)
from pr_review.storage.retention import LOCK_KEY, run_retention

pytestmark = pytest.mark.docker
ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 23, tzinfo=UTC)
OLD, NEW = NOW - timedelta(days=45), NOW - timedelta(days=2)
CHILD_TABLES = ("stage_attempts", "test_runs", "findings", "llm_calls", "comments", "events")


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    if shutil.which("docker") is None or _docker("info").returncode != 0:
        pytest.fail("Docker is required for retention tests")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    name = f"pr-review-ret-{uuid.uuid4().hex[:8]}"
    run = _docker(
        "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=pw",
        "-p", f"127.0.0.1:{port}:5432", "postgres:16-alpine",
    )  # fmt: skip
    if run.returncode != 0:
        pytest.fail(f"cannot start postgres: {run.stderr.strip()}")
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


@pytest.fixture
def factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    with engine.begin() as c:
        c.execute(text("truncate jobs, delivery_keys cascade"))
    yield sessionmaker(engine)


def _settings(days: int = 30) -> Settings:
    return Settings.model_construct(retention_days=days)


def _job(s: Session, status: str, when: datetime, children: bool = False) -> uuid.UUID:
    job = Job(
        repo="o/r", pr_number=1, head_sha=uuid.uuid4().hex, installation_id=1, status=status,
        created_at=when, updated_at=when, finished_at=when,
    )  # fmt: skip
    s.add(job)
    s.flush()
    if children:
        s.add(StageAttempt(job_id=job.id, stage="s", attempt_no=1, status="succeeded"))
        s.add(TestRun(job_id=job.id, framework="pytest"))
        f = Finding(
            job_id=job.id, agent_type="a", severity="low", category="c", summary="x",
            file_path="f.py", line_start=1, line_end=1, confidence=0.5,
        )  # fmt: skip
        s.add(f)
        s.add(LlmCall(job_id=job.id, model="m", outcome="ok"))
        s.flush()
        s.add(
            Comment(
                job_id=job.id, finding_id=f.id, github_comment_id=job.id.int % 10**12, kind="inline"
            )
        )
        s.add(Event(job_id=job.id, type="stage_started", payload={}))
    return job.id


def _count(s: Session, table: str) -> int:
    return s.scalar(text(f"select count(*) from {table}")) or 0  # noqa: S608


def test_purges_old_terminal_jobs_and_every_child_table(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    with factory() as s:
        for st in ("succeeded", "failed", "dead_letter", "superseded", "cancelled"):
            _job(s, st, OLD, children=True)
        keep_new = _job(s, "succeeded", NEW, children=True)
        keep_running = _job(s, "running", OLD, children=True)
        keep_queued = _job(s, "queued", OLD, children=True)
        s.commit()
    r = run_retention(factory, _settings(), NOW, workspace_root=tmp_path)
    assert r.counts["jobs"] == 5
    for t in CHILD_TABLES:
        assert r.counts[t] == 5
    with factory() as s:
        assert set(s.scalars(select(Job.id))) == {keep_new, keep_running, keep_queued}
        for t in CHILD_TABLES:
            assert _count(s, t) == 3, t
    again = run_retention(factory, _settings(), NOW, workspace_root=tmp_path)
    assert sum(again.counts.values()) == 0  # idempotent


def test_configurable_days(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as s:
        _job(s, "succeeded", NEW)
        s.commit()
    run_retention(factory, _settings(30), NOW, workspace_root=tmp_path)
    with factory() as s:
        assert _count(s, "jobs") == 1
    run_retention(factory, _settings(1), NOW, workspace_root=tmp_path)
    with factory() as s:
        assert _count(s, "jobs") == 0


def test_delivery_key_rule(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as s:
        for did, age in (("old", 45), ("redeliverable", 3), ("mid", 10)):
            s.add(DeliveryKey(delivery_id=did, created_at=NOW - timedelta(days=age)))
        s.commit()
    # retention 2 days is below the 7-day redelivery floor: only the 10 and 45 day keys go
    r = run_retention(factory, _settings(2), NOW, workspace_root=tmp_path)
    assert r.counts["delivery_keys"] == 2
    with factory() as s:
        assert set(s.scalars(select(DeliveryKey.delivery_id))) == {"redeliverable"}


def test_batches_many_rows(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as s:
        s.execute(
            text(
                "insert into jobs (id, repo, pr_number, head_sha, installation_id, status,"
                " created_at, updated_at, finished_at)"
                " select gen_random_uuid(), 'o/r', g, 'sha' || g, 1, 'succeeded', :t, :t, :t"
                " from generate_series(1, 2500) g"
            ),
            {"t": OLD},
        )
        s.commit()
    r = run_retention(factory, _settings(), NOW, batch_size=100, workspace_root=tmp_path)
    assert r.counts["jobs"] == 2500
    with factory() as s:
        assert _count(s, "jobs") == 0


def test_concurrent_runs_do_not_double_delete(
    factory: sessionmaker[Session], tmp_path: Path
) -> None:
    with factory() as s:
        for _ in range(300):
            _job(s, "succeeded", OLD, children=True)
        s.commit()
    reports = []
    errors: list[Exception] = []

    def work() -> None:
        try:
            reports.append(
                run_retention(factory, _settings(), NOW, batch_size=20, workspace_root=tmp_path)
            )
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sum(r.counts["jobs"] for r in reports) == 300
    with factory() as s:
        assert _count(s, "jobs") == 0


def test_skips_when_lock_held(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as s:
        _job(s, "succeeded", OLD)
        s.commit()
    with factory() as holder:
        holder.execute(text("select pg_advisory_lock(:k)"), {"k": LOCK_KEY})
        r = run_retention(factory, _settings(), NOW, workspace_root=tmp_path)
        holder.execute(text("select pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
    assert r.skipped
    with factory() as s:
        assert _count(s, "jobs") == 1


def test_events_only_removed_by_cascade(factory: sessionmaker[Session], tmp_path: Path) -> None:
    with factory() as s:
        _job(s, "succeeded", NEW, children=True)
        s.commit()
    with factory() as s:
        with pytest.raises(AppendOnlyViolation):
            s.execute(delete(Event))
    with factory() as s:
        ev = s.scalars(select(Event)).one()
        s.delete(ev)
        with pytest.raises(AppendOnlyViolation):
            s.flush()
    with factory() as s:
        _job(s, "failed", OLD, children=True)
        s.commit()
    r = run_retention(factory, _settings(), NOW, workspace_root=tmp_path)
    assert r.counts["events"] == 1
    with factory() as s:
        assert s.scalar(select(func.count()).select_from(Event)) == 1


def test_old_workspaces_removed_new_kept(factory: sessionmaker[Session], tmp_path: Path) -> None:
    old, new, other = tmp_path / "prr-ws-old", tmp_path / "prr-ws-new", tmp_path / "unrelated"
    for d in (old, new, other):
        (d / "src").mkdir(parents=True)
        (d / "src" / "a.py").write_text("x")
    long_ago = time.time() - 40 * 86400
    os.utime(old, (long_ago, long_ago))
    os.utime(other, (long_ago, long_ago))
    r = run_retention(factory, _settings(), NOW, workspace_root=tmp_path)
    assert r.workspaces_removed == 1
    assert not old.exists() and new.exists() and other.exists()


def test_workspace_of_late_running_job_kept(factory: sessionmaker[Session], tmp_path: Path) -> None:
    busy = tmp_path / "prr-ws-busy"
    busy.mkdir()
    t = time.time() - 5400  # older than 1h, younger than twice the 3600s job deadline
    os.utime(busy, (t, t))
    r = run_retention(factory, _settings(), NOW, workspace_root=tmp_path)
    assert r.workspaces_removed == 0 and busy.exists()
