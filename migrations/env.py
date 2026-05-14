"""Alembic env: async-aware, sources DB URL from the environment.

The migration runtime is intentionally decoupled from the application
runtime; it boots SQLAlchemy directly (not asyncpg) because alembic's
operations API targets a SQLAlchemy Connection. Production deployments
should set ``META_AGENT_DB_URL`` to a ``postgresql+asyncpg://`` URL.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


_DEFAULT_DB_URL = "postgresql+asyncpg://meta_agent:meta_agent@localhost:5432/meta_agent"
DB_URL_ENV = "META_AGENT_DB_URL"


def _resolved_db_url() -> str:
    url = os.environ.get(DB_URL_ENV, _DEFAULT_DB_URL)
    # Allow callers to pass a plain ``postgresql://`` URL and we coerce
    # to the async driver implicitly. Anything else is left untouched.
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


# Alembic uses ``target_metadata`` to drive autogenerate. We write
# migrations by hand here, so an empty metadata is sufficient.
target_metadata = None


def run_migrations_offline() -> None:
    """Run migrations rendering SQL without an active DB connection."""
    context.configure(
        url=_resolved_db_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_migrations_online_async() -> None:
    cfg = config.get_section(config.config_ini_section) or {}
    cfg["sqlalchemy.url"] = _resolved_db_url()
    connectable = async_engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations against an active async connection."""
    asyncio.run(_run_migrations_online_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
