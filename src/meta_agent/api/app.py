"""FastAPI application factory.

``create_app()`` is the single entry-point.  Pass ``lifespan=None`` in
unit tests to skip real Postgres / Redis connections; use
``app.dependency_overrides`` to inject fakes for every collaborator.

Environment variables read by the default lifespan:
- ``META_AGENT_DB_URL``    asyncpg DSN (default: local dev URL)
- ``META_AGENT_REDIS_URL`` redis-py URL (default: redis://localhost:6379/0)
- ``META_AGENT_TASK_TOPIC`` Redis stream topic (default: task.commands)
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from redis.asyncio import Redis

from meta_agent.api.routers import tasks as tasks_router
from meta_agent.infra.persistence import build_pool
from meta_agent.infra.persistence.pool import PoolConfig
from meta_agent.infra.queue import RedisStreamPublisher

logger = logging.getLogger(__name__)

_DB_URL_ENV = "META_AGENT_DB_URL"
_REDIS_URL_ENV = "META_AGENT_REDIS_URL"
_TOPIC_ENV = "META_AGENT_TASK_TOPIC"

_DEFAULT_DB_URL = "postgresql://meta_agent:dev-only@localhost:5432/meta_agent"
_DEFAULT_REDIS_URL = "redis://localhost:6379/0"
_DEFAULT_TASK_TOPIC = "task.commands"


@asynccontextmanager
async def _default_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open shared Postgres pool and Redis client; close on shutdown."""
    db_url = os.environ.get(_DB_URL_ENV, _DEFAULT_DB_URL)
    redis_url = os.environ.get(_REDIS_URL_ENV, _DEFAULT_REDIS_URL)
    task_topic = os.environ.get(_TOPIC_ENV, _DEFAULT_TASK_TOPIC)

    logger.info("api.startup db_url=%s redis_url=%s topic=%s", db_url, redis_url, task_topic)

    pool = await build_pool(PoolConfig(dsn=db_url, min_size=1, max_size=10))
    redis_client: Redis = Redis.from_url(redis_url, decode_responses=False)
    publisher = RedisStreamPublisher(redis_client)

    app.state.db_pool = pool
    app.state.redis = redis_client
    app.state.publisher = publisher
    app.state.task_topic = task_topic

    try:
        yield
    finally:
        logger.info("api.shutdown")
        await pool.close()
        await redis_client.aclose()


def create_app(*, lifespan: Any = _default_lifespan) -> FastAPI:
    """Return a configured :class:`FastAPI` application.

    Args:
        lifespan: ASGI lifespan context manager.  Pass ``None`` to skip
            real infrastructure connections (useful in tests).
    """
    app = FastAPI(
        title="meta-agent",
        description="Enterprise Code Agent – task API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.include_router(tasks_router.router, prefix="/v1")

    @app.get("/health", tags=["ops"], summary="Health check")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


# Module-level instance for ``uvicorn meta_agent.api.app:app``.
app = create_app()
