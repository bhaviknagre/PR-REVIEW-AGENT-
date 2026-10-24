"""Application factory; the startup point where secrets are registered for log redaction.

Startup (lifespan) builds the DB engine, Redis client and job enqueuer and exposes them on
``app.state``; each can be injected so tests need no real Postgres or Redis.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI
from redis.asyncio import Redis
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker

from pr_review.api import admin, health
from pr_review.api.ratelimit import RateLimiter
from pr_review.api.webhook import Enqueuer, router
from pr_review.core.logging import configure_logging
from pr_review.core.redaction import register_settings
from pr_review.core.settings import Settings, get_settings
from pr_review.pipeline.queue import create_enqueuer

log = logging.getLogger(__name__)
TIMELINE_PATH = "/jobs/{job_id}/timeline"


def _public_health_router() -> APIRouter:
    """health.router minus the timeline, which is served only under /admin (token required)."""
    public = APIRouter()
    public.routes.extend(r for r in health.router.routes if getattr(r, "path", "") != TIMELINE_PATH)
    return public


def create_app(
    settings: Settings | None = None,
    *,
    engine: Engine | None = None,
    redis: Redis | None = None,
    enqueuer: Enqueuer | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging()
    register_settings(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        eng = engine or create_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
        r: Any = redis or Redis.from_url(settings.redis_url.get_secret_value())
        enq = enqueuer or await create_enqueuer()
        factory = sessionmaker(eng, expire_on_commit=False)
        st = app.state
        st.session_factory, st.enqueuer = factory, enq
        st.db_check, st.redis_check = health.db_checker(factory), health.redis_checker(r)
        st.limiter = RateLimiter(r)
        try:
            yield
        finally:
            pool = None if enqueuer else getattr(enq, "_pool", None)
            if pool is not None:
                await pool.aclose()
            if redis is None:
                await r.aclose()
            if engine is None:
                eng.dispose()

    app = FastAPI(lifespan=lifespan)
    app.state.settings = settings
    app.include_router(router)
    app.include_router(_public_health_router())
    app.include_router(admin.router)
    return app
