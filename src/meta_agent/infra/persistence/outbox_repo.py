"""PostgreSQL implementation of :class:`OutboxRepository`.

The dispatcher (Batch F) drives this repo through a claim/dispatch loop:
``claim_pending`` atomically picks a batch of due rows and marks them
``in_flight`` via ``SELECT ... FOR UPDATE SKIP LOCKED`` so multiple
dispatcher replicas can run safely. Successful relays flip status to
``dispatched``; failures bump ``attempts`` and either reschedule or
mark the row terminal.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from meta_agent.core.domain.outbox import OutboxEvent, OutboxStatus
from meta_agent.core.ports.repository import OutboxRepository
from meta_agent.infra.persistence._guard import check_tenant
from meta_agent.infra.persistence.pool import DatabasePool


def _row_to_event(row: dict[str, Any]) -> OutboxEvent:
    return OutboxEvent(
        event_id=row["event_id"],
        tenant_id=row["tenant_id"],
        trace_id=row["trace_id"],
        aggregate_type=row["aggregate_type"],
        aggregate_id=row["aggregate_id"],
        topic=row["topic"],
        payload=row["payload"],
        idempotency_key=row["idempotency_key"],
        status=OutboxStatus(row["status"]),
        attempts=row["attempts"],
        created_at=row["created_at"],
        dispatched_at=row["dispatched_at"],
    )


class PgOutboxRepository(OutboxRepository):
    """asyncpg-backed :class:`OutboxRepository`."""

    _ENQUEUE = """
        INSERT INTO outbox_events (
            event_id, tenant_id, trace_id, aggregate_type, aggregate_id,
            topic, payload, idempotency_key, status, attempts,
            created_at, dispatched_at, next_attempt_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12, $11)
    """

    _CLAIM = """
        SELECT * FROM outbox_events
        WHERE status = 'pending'
          AND (next_attempt_at IS NULL OR next_attempt_at <= $1)
        ORDER BY created_at
        LIMIT $2
        FOR UPDATE SKIP LOCKED
    """

    _MARK_DISPATCHED = (
        "UPDATE outbox_events SET status = 'dispatched', "
        "dispatched_at = $2, last_error = NULL "
        "WHERE event_id = $1"
    )

    _MARK_FAILED = (
        "UPDATE outbox_events SET status = $4, attempts = attempts + 1, "
        "last_error = $2, next_attempt_at = $3 "
        "WHERE event_id = $1"
    )

    _GET = "SELECT * FROM outbox_events WHERE event_id = $1"

    _COUNT = "SELECT count(*) FROM outbox_events WHERE tenant_id = $1 AND status = $2"

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    async def enqueue(self, event: OutboxEvent) -> None:
        check_tenant(event.tenant_id)
        async with self._pool.acquire() as conn:
            await conn.execute(
                self._ENQUEUE,
                event.event_id,
                event.tenant_id,
                event.trace_id,
                event.aggregate_type,
                event.aggregate_id,
                event.topic,
                event.payload,
                event.idempotency_key,
                event.status.value,
                event.attempts,
                event.created_at,
                event.dispatched_at,
            )

    async def claim_pending(
        self,
        *,
        batch_size: int,
        now: datetime,
    ) -> list[OutboxEvent]:
        async with self._pool.transaction() as conn:
            rows = await conn.fetch(self._CLAIM, now, batch_size)
        return [_row_to_event(dict(r)) for r in rows]

    async def mark_dispatched(
        self,
        event_id: str,
        *,
        dispatched_at: datetime,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(self._MARK_DISPATCHED, event_id, dispatched_at)

    async def mark_failed(
        self,
        event_id: str,
        *,
        error: str,
        next_attempt_at: datetime | None,
        terminal: bool,
    ) -> None:
        status = OutboxStatus.FAILED.value if terminal else OutboxStatus.PENDING.value
        async with self._pool.acquire() as conn:
            await conn.execute(self._MARK_FAILED, event_id, error, next_attempt_at, status)

    async def get(self, event_id: str) -> OutboxEvent | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(self._GET, event_id)
        return _row_to_event(dict(row)) if row else None

    async def count_by_status(self, tenant_id: str, status: OutboxStatus) -> int:
        check_tenant(tenant_id)
        async with self._pool.acquire() as conn:
            value = await conn.fetchval(self._COUNT, tenant_id, status.value)
        return int(value or 0)
