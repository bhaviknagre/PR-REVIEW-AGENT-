import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pr_review.storage import models as m

ROOT = Path(__file__).resolve().parents[2]
TABLES = {
    "jobs", "delivery_keys", "stage_attempts", "test_runs", "findings",
    "llm_calls", "comments", "events",
}  # fmt: skip
FORBIDDEN = {"source", "diff", "patch", "content", "code", "prompt", "completion", "log"}


def _engine(tmp_path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    event.listen(engine, "connect", lambda c, _: c.execute("PRAGMA foreign_keys=ON"))
    return engine


def _migrate(engine: Engine, action: str, rev: str) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "src/pr_review/storage/migrations"))
    with engine.connect() as conn:
        cfg.attributes["connection"] = conn
        getattr(command, action)(cfg, rev)


def test_tables_and_no_source_columns() -> None:
    assert set(m.Base.metadata.tables) == TABLES
    for table in m.Base.metadata.tables.values():
        assert not FORBIDDEN & {c.name for c in table.columns}, table.name


def test_timestamps_aware_and_created_at_indexed() -> None:
    for table in m.Base.metadata.tables.values():
        col = table.c.created_at
        assert col.type.timezone is True  # type: ignore[attr-defined]
        assert any(list(i.columns) == [col] for i in table.indexes), table.name


def test_migration_matches_models_and_downgrades(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _migrate(engine, "upgrade", "head")
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), m.Base.metadata)
    assert diff == []
    _migrate(engine, "downgrade", "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}


def test_constraints_and_cascade(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _migrate(engine, "upgrade", "head")
    job = m.Job(repo="o/r", pr_number=1, head_sha="abc", installation_id=1)
    with Session(engine) as s:
        s.add(job)
        s.flush()
        s.add(m.DeliveryKey(delivery_id="d1", job_id=job.id))
        s.add(m.Event(job_id=job.id, type="x", payload={"k": 1}))
        s.commit()
        assert job.created_at is not None
        s.add(m.DeliveryKey(delivery_id="d1"))  # replayed delivery
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        s.add(m.Job(repo="o/r", pr_number=1, head_sha="abc", installation_id=1))  # same SHA
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        s.add(m.Event(job_id=uuid.uuid4(), type="x"))  # orphan
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        s.delete(s.get(m.Job, job.id))
        s.commit()
        assert s.query(m.Event).count() == 0
        assert s.get(m.DeliveryKey, "d1").job_id is None  # type: ignore[union-attr]
