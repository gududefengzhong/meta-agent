"""asyncpg connection pool wrapper.

The pool registers a ``jsonb`` codec so callers receive ``dict`` /
``list`` directly without manual ``json.loads``. The wrapper exposes
only what business code needs (``acquire``, ``close``) so the rest of
the codebase is insulated from asyncpg specifics.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import asyncpg


async def _init_connection(conn: asyncpg.Connection[Any]) -> None:
    """Per-connection initialization: register JSON codecs."""
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
        format="text",
    )
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
        format="text",
    )


@dataclass(frozen=True, slots=True)
class PoolConfig:
    """Configuration knobs for :func:`build_pool`.

    ``dsn`` follows libpq conventions (``postgresql://user:pass@host/db``).
    ``min_size`` / ``max_size`` bound the pool; the defaults are tuned
    for a single Worker replica and should be revisited under load.
    """

    dsn: str
    min_size: int = 1
    max_size: int = 10
    command_timeout: float = 30.0


class DatabasePool:
    """Lifecycle-managed wrapper around :class:`asyncpg.Pool`."""

    def __init__(self, pool: asyncpg.Pool[Any]) -> None:
        self._pool = pool

    @property
    def raw(self) -> asyncpg.Pool[Any]:
        """Expose the underlying pool. Intended for advanced cases only."""
        return self._pool

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[asyncpg.Connection[Any]]:
        """Acquire a connection for the scope of the ``async with``."""
        conn = await self._pool.acquire()
        try:
            yield conn
        finally:
            await self._pool.release(conn)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection[Any]]:
        """Acquire a connection bound inside a single PG transaction."""
        async with self.acquire() as conn, conn.transaction():
            yield conn

    async def close(self) -> None:
        """Close the pool. Safe to call multiple times."""
        await self._pool.close()


async def build_pool(config: PoolConfig) -> DatabasePool:
    """Construct a :class:`DatabasePool` from ``config``.

    Note: callers are responsible for closing the returned pool at
    process shutdown via :meth:`DatabasePool.close`.
    """
    pool = await asyncpg.create_pool(
        dsn=config.dsn,
        min_size=config.min_size,
        max_size=config.max_size,
        command_timeout=config.command_timeout,
        init=_init_connection,
    )
    if pool is None:  # pragma: no cover - asyncpg returns None only on bad config
        raise RuntimeError("asyncpg.create_pool returned None")
    return DatabasePool(pool)
