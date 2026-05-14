"""PostgreSQL implementation of :class:`AuditRepository`."""

from __future__ import annotations

from typing import Any

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.ports.repository import AuditRepository
from meta_agent.infra.persistence._guard import check_tenant
from meta_agent.infra.persistence.pool import DatabasePool


def _row_to_event(row: dict[str, Any]) -> AuditEvent:
    return AuditEvent(
        event_id=row["event_id"],
        tenant_id=row["tenant_id"],
        principal_id=row["principal_id"],
        session_id=row["session_id"],
        task_id=row["task_id"],
        trace_id=row["trace_id"],
        action=row["action"],
        payload=row["payload"],
        occurred_at=row["occurred_at"],
    )


class PgAuditRepository(AuditRepository):
    """asyncpg-backed append-only :class:`AuditRepository`."""

    _APPEND = """
        INSERT INTO audit_events (
            event_id, tenant_id, principal_id, session_id, task_id,
            trace_id, action, payload, occurred_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
    """

    _LIST_RECENT = (
        "SELECT * FROM audit_events WHERE tenant_id = $1 ORDER BY occurred_at DESC LIMIT $2"
    )

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    async def append(self, event: AuditEvent) -> None:
        check_tenant(event.tenant_id)
        async with self._pool.acquire() as conn:
            await conn.execute(
                self._APPEND,
                event.event_id,
                event.tenant_id,
                event.principal_id,
                event.session_id,
                event.task_id,
                event.trace_id,
                event.action,
                event.payload,
                event.occurred_at,
            )

    async def list_recent(
        self,
        tenant_id: str,
        limit: int = 100,
    ) -> list[AuditEvent]:
        check_tenant(tenant_id)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(self._LIST_RECENT, tenant_id, limit)
        return [_row_to_event(dict(r)) for r in rows]
