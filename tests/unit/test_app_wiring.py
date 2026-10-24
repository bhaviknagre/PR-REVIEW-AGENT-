import hashlib
import hmac
import json
import uuid
from pathlib import Path

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select

from pr_review.api.app import create_app
from pr_review.core.settings import Settings
from pr_review.storage.models import Base, Job


class Enq:
    async def enqueue(self, job_id: uuid.UUID) -> None:
        return None


def make_settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        github_app_id=1,
        github_private_key="pk",  # type: ignore[arg-type]
        github_webhook_secret="whsec-test-secret",  # type: ignore[arg-type]
        database_url="postgresql://u:p@h/db",  # type: ignore[arg-type]
        redis_url="redis://h/0",  # type: ignore[arg-type]
    )


def signed(body: bytes) -> dict[str, str]:
    sig = "sha256=" + hmac.new(b"whsec-test-secret", body, hashlib.sha256).hexdigest()
    return {"X-Hub-Signature-256": sig, "X-GitHub-Event": "ping"}


def build(tmp_path: Path, redis: FakeAsyncRedis, enq: Enq):  # type: ignore[no-untyped-def]
    engine = create_engine(f"sqlite:///{tmp_path / 'w.db'}")
    Base.metadata.create_all(engine)
    return engine, create_app(make_settings(), engine=engine, redis=redis, enqueuer=enq)


def test_lifespan_sets_state_and_health(tmp_path: Path) -> None:
    redis, enq = FakeAsyncRedis(), Enq()
    engine, app = build(tmp_path, redis, enq)
    with TestClient(app) as c:
        assert app.state.enqueuer is enq
        assert app.state.session_factory and app.state.limiter
        r = c.get("/readyz")
        assert r.status_code == 200 and r.json()["checks"] == {"db": "ok", "redis": "ok"}
        assert c.get("/metrics").status_code == 200
        assert c.get(f"/jobs/{uuid.uuid4()}/timeline").status_code in (401, 404)
        assert (
            c.get(f"/admin/jobs/{uuid.uuid4()}/timeline").status_code == 503
        )  # no admin token: closed
    engine.dispose()


def test_shutdown_closes_owned_resources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[str] = []
    redis = FakeAsyncRedis()

    async def aclose() -> None:
        closed.append("redis")

    monkeypatch.setattr("pr_review.api.app.Redis.from_url", lambda url: redis)
    monkeypatch.setattr(redis, "aclose", aclose)
    url = f"sqlite:///{tmp_path / 'o.db'}"
    monkeypatch.setattr(
        "pr_review.api.app.create_engine", lambda u, **k: _Eng(create_engine(url), closed)
    )
    app = create_app(make_settings(), enqueuer=Enq())
    with TestClient(app):
        pass
    assert closed == ["redis", "engine"]


class _Eng:
    def __init__(self, eng, closed):  # type: ignore[no-untyped-def]
        self._e, self._c = eng, closed

    def __getattr__(self, n: str):  # type: ignore[no-untyped-def]
        return getattr(self._e, n)

    def dispose(self) -> None:
        self._c.append("engine")


def test_unsigned_flood_hits_ip_limit_and_installation_only_when_verified(tmp_path: Path) -> None:
    redis = FakeAsyncRedis()
    engine, app = build(tmp_path, redis, Enq())
    with TestClient(app) as c:
        app.state.limiter._ip_limit = 3
        app.state.limiter._installation_limit = 1
        codes = [c.post("/webhook/github", content=b"{}").status_code for _ in range(5)]
        assert codes == [401, 401, 401, 429, 429]
        r = c.post("/webhook/github", content=b"{}")
        assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
        assert not redis_keys(redis, "rl:inst:")
        app.state.limiter._ip_limit = 100
        body = json.dumps(
            {
                "action": "opened",
                "repository": {"full_name": "o/r"},
                "pull_request": {"number": 1, "head": {"sha": "abc"}},
                "installation": {"id": 7},
            }
        ).encode()
        h = signed(body) | {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d1"}
        assert c.post("/webhook/github", content=body, headers=h).status_code == 202
        h["X-GitHub-Delivery"] = "d2"
        assert c.post("/webhook/github", content=body, headers=h).status_code == 429
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(Job)) == 1
    engine.dispose()


def redis_keys(redis: FakeAsyncRedis, prefix: str) -> list[str]:
    import asyncio

    keys = asyncio.run(redis.keys(prefix + "*"))
    return [k.decode() for k in keys]
