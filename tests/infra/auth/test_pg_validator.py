"""Unit tests for :class:`PgTokenValidator` using a fake DB pool."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest

from meta_agent.core.ports.auth import AuthBackendError
from meta_agent.infra.auth.pg_validator import PgTokenValidator
from meta_agent.infra.persistence.pool import DatabasePool


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class _FakeConn:
    def __init__(
        self,
        rows: dict[str, dict[str, Any]],
        *,
        raise_on_fetch: BaseException | None = None,
        touches: list[str] | None = None,
    ) -> None:
        self._rows = rows
        self._raise = raise_on_fetch
        self.touches = touches if touches is not None else []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if self._raise is not None:
            raise self._raise
        token_hash = args[0]
        return self._rows.get(token_hash)

    async def execute(self, sql: str, *args: Any) -> None:
        self.touches.append(args[0])


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_FakeConn]:
        yield self._conn


def _make_pool(conn: _FakeConn) -> DatabasePool:
    # ``PgTokenValidator`` only uses ``pool.acquire()``; cast a fake to
    # ``DatabasePool`` so we don't depend on a real asyncpg pool.
    return cast(DatabasePool, _FakePool(conn))


async def test_empty_token_returns_none_without_db_access() -> None:
    conn = _FakeConn(rows={}, raise_on_fetch=AssertionError("must not hit db"))
    validator = PgTokenValidator(_make_pool(conn))
    assert await validator.validate("") is None


async def test_unknown_token_returns_none() -> None:
    conn = _FakeConn(rows={})
    validator = PgTokenValidator(_make_pool(conn))
    assert await validator.validate("tok-x") is None


async def test_known_token_returns_principal() -> None:
    token = "tok-a"
    conn = _FakeConn(
        rows={
            _hash(token): {
                "tenant_id": "tenant-1",
                "principal_id": "user-1",
                "scopes": ["read", "write"],
            }
        }
    )
    validator = PgTokenValidator(_make_pool(conn), touch_last_used=False)
    principal = await validator.validate(token)
    assert principal is not None
    assert principal.tenant_id == "tenant-1"
    assert principal.principal_id == "user-1"
    assert principal.scopes == ("read", "write")


async def test_null_scopes_resolves_to_empty_tuple() -> None:
    token = "tok-a"
    conn = _FakeConn(
        rows={
            _hash(token): {
                "tenant_id": "tenant-1",
                "principal_id": "user-1",
                "scopes": None,
            }
        }
    )
    validator = PgTokenValidator(_make_pool(conn), touch_last_used=False)
    principal = await validator.validate(token)
    assert principal is not None
    assert principal.scopes == ()


async def test_backend_error_wrapped_as_auth_backend_error() -> None:
    conn = _FakeConn(rows={}, raise_on_fetch=RuntimeError("pg down"))
    validator = PgTokenValidator(_make_pool(conn))
    with pytest.raises(AuthBackendError, match="api_keys lookup failed"):
        await validator.validate("anything")


async def test_touch_last_used_fires_when_enabled() -> None:
    token = "tok-a"
    touches: list[str] = []
    conn = _FakeConn(
        rows={
            _hash(token): {
                "tenant_id": "tenant-1",
                "principal_id": "user-1",
                "scopes": [],
            }
        },
        touches=touches,
    )
    validator = PgTokenValidator(_make_pool(conn), touch_last_used=True)
    principal = await validator.validate(token)
    assert principal is not None
    # touch runs as a fire-and-forget task; yield until it executes.
    for _ in range(10):
        await asyncio.sleep(0)
        if touches:
            break
    assert touches == [_hash(token)]


async def test_touch_last_used_skipped_when_disabled() -> None:
    token = "tok-a"
    touches: list[str] = []
    conn = _FakeConn(
        rows={
            _hash(token): {
                "tenant_id": "tenant-1",
                "principal_id": "user-1",
                "scopes": [],
            }
        },
        touches=touches,
    )
    validator = PgTokenValidator(_make_pool(conn), touch_last_used=False)
    await validator.validate(token)
    for _ in range(5):
        await asyncio.sleep(0)
    assert touches == []
