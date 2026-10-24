"""Crash recovery on real Postgres 16 (throwaway container): lease exclusivity, SKIP LOCKED,
expired-lease reclaim and resume, stale transition. Skipped when Docker is unavailable."""

import asyncio
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
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from pr_review.core.settings import WorkerConfig
from pr_review.pipeline import lease, queue, state
from pr_review.storage.models import Event, Job
from pr_review.storage.repositories import JobRepository

pytestmark = pytest.mark.docker
ROOT = Path(__file__).resolve().parents[2]
TTL = timedelta(minutes=5)
_PR = iter(range(1, 10_000_000))


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    if shutil.which("docker") is None or _docker("info").returncode != 0:
        pytest.skip("Docker unavailable")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    name = f"pr-review-rec-{uuid.uuid4().hex[:8]}"
    run = _docker(
        "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=pw",
        "-p", f"127.0.0.1:{port}:5432", "postgres:16-alpine",
    )  # fmt: skip
    if run.returncode != 0:
        pytest.skip(f"cannot start postgres container: {run.stderr.strip()}")
    try:
        eng = create_engine(
            f"postgresql+psycopg://postgres:pw@127.0.0.1:{port}/postgres",
            pool_size=20,
            max_overflow=20,
        )
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
def factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def _job(f: sessionmaker[Session]) -> uuid.UUID:
    with f() as s:
        job = JobRepository(s).create("o/r", next(_PR), uuid.uuid4().hex, 7)
        s.commit()
        return job.id


def _row(f: sessionmaker[Session], jid: uuid.UUID) -> tuple[str, str | None]:
    with f() as s:
        return s.execute(select(Job.status, Job.leased_by).where(Job.id == jid)).one()


def _n_events(f: sessionmaker[Session], jid: uuid.UUID) -> int:
    with f() as s:
        return s.scalar(select(func.count()).select_from(Event).where(Event.job_id == jid)) or 0


def _run_threads(targets: list[threading.Thread]) -> None:
    for t in targets:
        t.start()
    for t in targets:
        t.join(timeout=120)
    assert not any(t.is_alive() for t in targets), "deadlock/hang"


def _race_once(factory: sessionmaker[Session]) -> None:
    jid = _job(factory)
    barrier = threading.Barrier(8)
    results: dict[int, bool] = {}

    def claimer(i: int) -> None:
        with factory() as s:
            barrier.wait(timeout=30)
            results[i] = state.claim(s, jid, f"t{i}", TTL)

    _run_threads([threading.Thread(target=claimer, args=(i,)) for i in range(8)])
    winners = [i for i, ok in results.items() if ok]
    assert len(results) == 8 and len(winners) == 1
    assert _row(factory, jid) == ("running", f"t{winners[0]}")
    assert _n_events(factory, jid) == 1


def test_concurrent_claim_has_exactly_one_winner(factory: sessionmaker[Session]) -> None:
    for _ in range(10):
        _race_once(factory)


def test_skip_locked_distributes_each_job_once(factory: sessionmaker[Session]) -> None:
    ids = {_job(factory) for _ in range(20)}
    claimed: list[uuid.UUID] = []
    lock = threading.Lock()

    def claimer(name: str) -> None:
        while True:
            with factory() as s:
                queued = list(s.scalars(select(Job.id).where(Job.status == state.QUEUED)))
            queued = [j for j in queued if j in ids]
            if not queued:
                return
            for jid in queued:
                with factory() as s:
                    if state.claim(s, jid, name, TTL):
                        with lock:
                            claimed.append(jid)

    _run_threads([threading.Thread(target=claimer, args=(f"c{i}",)) for i in range(5)])
    assert sorted(claimed) == sorted(ids)  # every job exactly once


def test_expired_lease_is_reclaimed_and_resumes_after_last_completed_stage(
    factory: sessionmaker[Session],
) -> None:
    jid = _job(factory)
    done = ("fetch", "detect")
    with factory() as s:
        assert state.claim(s, jid, "doomed", TTL)
        for stage in state.STAGES[:3]:  # worker finishes two stages, dies inside the third
            attempt = state.start_stage(s, jid, stage)
            if stage in done:
                state.finish_stage(s, attempt, None, 1)
        s.execute(update(Job).where(Job.id == jid).values(lease_expires_at=datetime.now(UTC) - TTL))
        s.commit()
    sent: list[tuple[uuid.UUID, float]] = []

    async def enqueue(j: uuid.UUID, delay: float) -> None:
        sent.append((j, delay))

    assert asyncio.run(lease.recover_expired(factory, enqueue, WorkerConfig())) == 1
    assert _row(factory, jid) == ("queued", None) and [j for j, _ in sent] == [jid]

    calls: list[str] = []
    stages: dict[str, queue.Stage] = {}
    for name in state.STAGES:

        async def h(_: uuid.UUID, name: str = name) -> None:
            calls.append(name)

        stages[name] = h
    assert asyncio.run(queue.run_job(factory, stages, jid, "w2")) == "succeeded"
    assert calls == list(state.STAGES[2:])  # fetch and detect never re-run


def test_live_lease_is_not_recovered_and_stale_transition_rejected(
    factory: sessionmaker[Session],
) -> None:
    jid = _job(factory)
    with factory() as s:
        assert state.claim(s, jid, "alive", TTL)

    async def enqueue(_: uuid.UUID, __: float) -> None:
        raise AssertionError("live job must not be recovered")

    assert asyncio.run(lease.recover_expired(factory, enqueue, WorkerConfig())) == 0
    before = _n_events(factory, jid)
    with factory() as s, pytest.raises(state.TransitionError, match="no longer"):
        state.transition(s, jid, state.QUEUED, state.CANCELLED, "stale")
    assert _row(factory, jid) == ("running", "alive")
    assert _n_events(factory, jid) == before
