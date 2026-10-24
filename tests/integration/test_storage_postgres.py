"""Real Postgres 16 checks (throwaway container). Skipped when Docker is unavailable."""

import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, delete, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from pr_review.storage.events import AppendOnlyViolation, EventRepository, StageStartedV1
from pr_review.storage.models import Event, Job
from pr_review.storage.repositories import DeliveryKeyRepository, JobRepository

pytestmark = pytest.mark.docker
ROOT = Path(__file__).resolve().parents[2]


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
        url = f"postgresql+psycopg://postgres:pw@127.0.0.1:{port}/postgres"
        eng = create_engine(url)
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


def test_duplicate_delivery_reports_already_seen(engine: Engine) -> None:
    with Session(engine) as s:
        keys = DeliveryKeyRepository(s)
        assert keys.record("d-1") is True
        assert keys.record("d-1") is False


def test_event_update_and_delete_rejected(engine: Engine) -> None:
    with Session(engine) as s:
        job = JobRepository(s).create("o/r", 1, "aaa", 7)
        ev = EventRepository(s).append(job.id, StageStartedV1(attempt_no=1), stage="fetch")
        s.flush()
        with pytest.raises(AppendOnlyViolation):
            s.execute(update(Event).where(Event.id == ev.id).values(stage="x"))
        with pytest.raises(AppendOnlyViolation):
            s.execute(delete(Event).where(Event.id == ev.id))
        s.rollback()


def test_job_delete_cascades_events(engine: Engine) -> None:
    with Session(engine) as s:
        job = JobRepository(s).create("o/r", 2, "bbb", 7)
        EventRepository(s).append(job.id, StageStartedV1(attempt_no=1), stage="fetch")
        s.commit()
        s.execute(delete(Job).where(Job.id == job.id))
        s.commit()
        assert s.scalar(select(func.count()).select_from(Event).where(Event.job_id == job.id)) == 0


def test_admin_requeue_state_machine(engine: Engine) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from pr_review.api.admin import requeue

    with Session(engine) as s:
        dead = JobRepository(s).create("o/r", 50, "dl1", 7)
        live = JobRepository(s).create("o/r", 51, "dl2", 7)
        s.execute(
            update(Job)
            .where(Job.id == dead.id)
            .values(status="dead_letter", dead_letter_reason="boom", finished_at=func.now())
        )
        s.commit()
        dead_id, live_id = dead.id, live.id

    def attempt(_: int) -> bool:
        with Session(engine) as s2:
            return requeue(s2, dead_id)

    with ThreadPoolExecutor(2) as ex:
        results = list(ex.map(attempt, range(2)))
    assert sorted(results) == [False, True]  # exactly one concurrent requeue wins
    with Session(engine) as s:
        row = s.get(Job, dead_id)
        assert row is not None and row.status == "queued"
        assert row.dead_letter_reason is None and row.finished_at is None
        n = s.scalar(select(func.count()).select_from(Event).where(Event.job_id == dead_id))
        assert n == 1
        assert requeue(s, dead_id) is False  # no longer dead-lettered
        assert requeue(s, live_id) is False  # never dead-lettered
        assert s.get(Job, live_id).status == "queued"  # type: ignore[union-attr]
