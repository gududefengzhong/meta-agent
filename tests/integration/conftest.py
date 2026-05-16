"""Integration-test fixtures backed by testcontainers or CI services.

Each session uses one Postgres and one Redis instance:
- If ``META_AGENT_CI_PG_DSN`` / ``META_AGENT_CI_REDIS_URL`` are set
  (CI service-containers mode), those URLs are used directly.
- Otherwise testcontainers spins up ephemeral containers (local dev).

The Postgres instance is migrated to ``head`` via alembic so all
repositories see the real schema. Each test function gets a freshly
truncated DB and a flushed Redis to keep tests independent.

Skipping rule: if neither CI env vars nor a reachable Docker daemon
are available, every test under ``tests/integration`` is skipped
with a clear reason rather than failing.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

# Disable Ryuk reaper before importing testcontainers; on Mac/Colima
# setups the reaper port mapping is often unavailable and crashes
# container startup. Tests manage container teardown explicitly via
# the ``with`` block in each fixture.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis

from meta_agent.infra.persistence.pool import DatabasePool, PoolConfig, build_pool

_CI_PG_DSN_ENV = "META_AGENT_CI_PG_DSN"
_CI_REDIS_URL_ENV = "META_AGENT_CI_REDIS_URL"


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
    except Exception:
        return False
    return True


def _ci_services_available() -> bool:
    return bool(os.environ.get(_CI_PG_DSN_ENV) and os.environ.get(_CI_REDIS_URL_ENV))


pytestmark = pytest.mark.integration

if not _ci_services_available() and not _docker_available():
    pytest.skip(
        "neither CI service env vars nor a docker daemon are available; skipping integration tests",
        allow_module_level=True,
    )


_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    """Provide a Postgres DSN, preferring CI-provided service over testcontainers."""
    ci_dsn = os.environ.get(_CI_PG_DSN_ENV)
    if ci_dsn:
        yield ci_dsn
        return

    from testcontainers.core.exceptions import ContainerStartException
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(
        "postgres:16-alpine",
        username="meta_agent",
        password="dev-only",
        dbname="meta_agent",
        driver=None,
    )
    try:
        with container as pg:
            yield str(pg.get_connection_url())
    except ContainerStartException as exc:  # pragma: no cover - env issue
        pytest.skip(f"failed to start postgres container: {exc}")


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    """Provide a Redis URL, preferring CI-provided service over testcontainers."""
    ci_url = os.environ.get(_CI_REDIS_URL_ENV)
    if ci_url:
        yield ci_url
        return

    from testcontainers.core.exceptions import ContainerStartException
    from testcontainers.redis import RedisContainer

    try:
        with RedisContainer("redis:7-alpine") as rc:
            host = rc.get_container_host_ip()
            port = rc.get_exposed_port(6379)
            yield f"redis://{host}:{port}/0"
    except ContainerStartException as exc:  # pragma: no cover - env issue
        pytest.skip(f"failed to start redis container: {exc}")


@pytest.fixture(scope="session", autouse=True)
def _apply_migrations(pg_dsn: str) -> None:
    cfg = Config(str(_REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_REPO_ROOT / "migrations"))
    os.environ["META_AGENT_DB_URL"] = pg_dsn
    command.upgrade(cfg, "head")


@pytest_asyncio.fixture
async def db_pool(pg_dsn: str) -> AsyncIterator[DatabasePool]:
    pool = await build_pool(PoolConfig(dsn=pg_dsn, min_size=1, max_size=4))
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "TRUNCATE tasks, sessions, outbox_events, audit_events, "
                "task_checkpoints, llm_usage_logs RESTART IDENTITY CASCADE"
            )
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def redis_client(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=False)
    try:
        await client.flushdb()
        yield client
    finally:
        await client.aclose()
