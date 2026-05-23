"""PostgreSQL implementation of :class:`SessionRepository`."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from meta_agent.core.domain.session import Session
from meta_agent.core.ports.repository import SessionRepository
from meta_agent.infra.persistence._guard import check_tenant
from meta_agent.infra.persistence.pool import DatabasePool


def _row_to_session(row: dict[str, Any]) -> Session:
    return Session(
        session_id=row["session_id"],
        tenant_id=row["tenant_id"],
        principal_id=row["principal_id"],
        created_at=row["created_at"],
        last_active_at=row["last_active_at"],
        is_closed=row["is_closed"],
    )


class PgSessionRepository(SessionRepository):
    """asyncpg-backed :class:`SessionRepository`."""

    _UPSERT = """
        INSERT INTO sessions (
            session_id, tenant_id, principal_id,
            created_at, last_active_at, is_closed
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (session_id) DO UPDATE SET
            last_active_at = EXCLUDED.last_active_at,
            is_closed = EXCLUDED.is_closed
    """

    _GET = "SELECT * FROM sessions WHERE tenant_id = $1 AND session_id = $2"

    _TOUCH = "UPDATE sessions SET last_active_at = $3 WHERE tenant_id = $1 AND session_id = $2"

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    async def upsert(self, session: Session) -> None:
        check_tenant(session.tenant_id)
        async with self._pool.acquire() as conn:
            await self._exec_upsert(session, conn)

    async def upsert_in_conn(self, session: Session, conn: Any) -> None:
        """Same as :meth:`upsert` but on a caller-owned connection.

        Used by ``POST /v1/tasks`` to upsert the Session row in the
        same DB transaction as the task + outbox writes, so a task
        submission for a fresh session is atomic with the session
        record's existence.
        """
        check_tenant(session.tenant_id)
        await self._exec_upsert(session, conn)

    async def _exec_upsert(self, session: Session, conn: Any) -> None:
        await conn.execute(
            self._UPSERT,
            session.session_id,
            session.tenant_id,
            session.principal_id,
            session.created_at,
            session.last_active_at,
            session.is_closed,
        )

    async def get(self, tenant_id: str, session_id: str) -> Session | None:
        check_tenant(tenant_id)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(self._GET, tenant_id, session_id)
        return _row_to_session(dict(row)) if row else None

    async def touch(
        self,
        tenant_id: str,
        session_id: str,
        last_active_at: datetime,
    ) -> None:
        check_tenant(tenant_id)
        async with self._pool.acquire() as conn:
            await conn.execute(self._TOUCH, tenant_id, session_id, last_active_at)
