from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from pr_review.api.webhook import _persist
from pr_review.pipeline.supersede import is_cancelled, supersede_older
from pr_review.storage.events import DecisionV1, EventRepository
from pr_review.storage.models import Base, Job
from pr_review.storage.repositories import JobRepository


@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _new(s: Session, sha: str, pr: int = 1, repo: str = "o/r", status: str = "queued") -> Job:
    job = JobRepository(s).create(repo, pr, sha, 7)
    job.status = status
    s.flush()
    return job


def test_new_sha_supersedes_queued_and_running_with_event(session: Session) -> None:
    queued, running = _new(session, "a"), _new(session, "b", status="running")
    newest = _new(session, "c")
    assert set(supersede_older(session, newest)) == {queued.id, running.id}
    session.refresh(queued)
    assert queued.status == "superseded" and queued.finished_at is not None
    assert newest.status == "queued"
    ((_, payload),) = EventRepository(session).timeline(running.id)
    assert isinstance(payload, DecisionV1) and payload.name == "superseded"
    assert "c" in payload.reason


def test_leaves_finished_other_prs_and_other_repos(session: Session) -> None:
    done = _new(session, "a", status="succeeded")
    other_pr, other_repo = _new(session, "a", pr=2), _new(session, "a", repo="x/y")
    assert supersede_older(session, _new(session, "b")) == []
    assert [j.status for j in (done, other_pr, other_repo)] == ["succeeded", "queued", "queued"]


def test_is_cancelled_sees_other_session_update(session: Session) -> None:
    old = _new(session, "a")
    assert not is_cancelled(session, old.id)
    supersede_older(session, _new(session, "b"))
    assert is_cancelled(session, old.id)  # identity map still holds stale 'queued'


def test_last_accepted_wins(session: Session) -> None:
    first, second = _new(session, "a"), _new(session, "b")
    supersede_older(session, first)  # first is accepted after second: it becomes newest
    assert session.scalars(select(Job.status).order_by(Job.head_sha)).all() == [
        "queued",
        "superseded",
    ]
    assert second.id != first.id


def test_persist_wires_supersede_and_dedup(session: Session) -> None:
    factory = sessionmaker(session.get_bind())
    first = _persist(factory, "d1", "o/r", 1, "a", 7)
    assert _persist(factory, "d1", "o/r", 1, "a", 7) is None  # same delivery
    assert _persist(factory, "d2", "o/r", 1, "a", 7) is None  # same SHA, new delivery
    second = _persist(factory, "d3", "o/r", 1, "b", 7)
    assert first and second
    assert is_cancelled(session, first) and not is_cancelled(session, second)
