"""PostgreSQL implementation of :class:`AuditRepository`."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.ports.repository import AuditFilter, AuditRepository
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

    async def list_for_task_since(
        self,
        tenant_id: str,
        task_id: str,
        *,
        after: tuple[datetime, str] | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """ASC stream of audit events for one task, keyset-paginated by
        ``(occurred_at, event_id)`` so callers can replay without
        re-emitting an event they've already seen."""

        check_tenant(tenant_id)
        params: list[Any] = [tenant_id, task_id]
        clauses = ["tenant_id = $1", "task_id = $2"]
        if after is not None:
            cursor_at, cursor_id = after
            params.append(cursor_at)
            cursor_at_n = len(params)
            params.append(cursor_id)
            cursor_id_n = len(params)
            # Strict ASC keyset: the next page starts strictly after
            # the supplied cursor, breaking ties on event_id so
            # ordering is stable when two events share a timestamp.
            clauses.append(
                f"(occurred_at > ${cursor_at_n} "
                f"OR (occurred_at = ${cursor_at_n} AND event_id > ${cursor_id_n}))"
            )
        params.append(limit)
        limit_n = len(params)
        sql = (
            f"SELECT * FROM audit_events WHERE {' AND '.join(clauses)} "
            f"ORDER BY occurred_at ASC, event_id ASC LIMIT ${limit_n}"
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [_row_to_event(dict(r)) for r in rows]

    async def list_filtered(
        self,
        tenant_id: str,
        filt: AuditFilter,
    ) -> list[AuditEvent]:
        check_tenant(tenant_id)
        # ``$1..$3`` always bind tenant_id / since / until; optional
        # filters and the keyset cursor append further params so the
        # ix_audit_tenant_occurred index keeps driving the plan from a
        # fixed prefix.
        params: list[Any] = [tenant_id, filt.since, filt.until]
        clauses: list[str] = ["tenant_id = $1", "occurred_at >= $2", "occurred_at < $3"]
        if filt.action is not None:
            params.append(filt.action)
            clauses.append(f"action = ${len(params)}")
        if filt.task_id is not None:
            params.append(filt.task_id)
            clauses.append(f"task_id = ${len(params)}")
        if filt.before is not None:
            cursor_at, cursor_id = filt.before
            params.append(cursor_at)
            cursor_at_n = len(params)
            params.append(cursor_id)
            cursor_id_n = len(params)
            # Strict keyset (DESC): next page starts before the previous
            # row, breaking ties on event_id to keep ordering stable.
            clauses.append(
                f"(occurred_at < ${cursor_at_n} "
                f"OR (occurred_at = ${cursor_at_n} AND event_id < ${cursor_id_n}))"
            )
        params.append(filt.limit)
        limit_n = len(params)
        sql = (
            f"SELECT * FROM audit_events WHERE {' AND '.join(clauses)} "
            f"ORDER BY occurred_at DESC, event_id DESC LIMIT ${limit_n}"
        )
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        return [_row_to_event(dict(r)) for r in rows]
