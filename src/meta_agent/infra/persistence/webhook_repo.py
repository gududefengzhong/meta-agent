"""asyncpg-backed webhook subscription + delivery repositories (Phase γ-B-2).

Two adapters cohabit here because they share the JSONB / TEXT[] codec
quirks and benefit from being read together when reasoning about the
``ON CONFLICT`` / unique-key surface. They are otherwise independent:
``PgWebhookSubscriptionRepository`` is consulted only at fanout time;
``PgWebhookDeliveryRepository`` carries the dispatcher's hot path.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import asyncpg

from meta_agent.core.domain.webhook import (
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookSubscription,
)
from meta_agent.core.ports.webhook import (
    WebhookDeliveryRepository,
    WebhookSubscriptionRepository,
)
from meta_agent.infra.persistence._guard import check_tenant
from meta_agent.infra.persistence.pool import DatabasePool


def _row_to_subscription(row: dict[str, Any]) -> WebhookSubscription:
    return WebhookSubscription(
        subscription_id=row["subscription_id"],
        tenant_id=row["tenant_id"],
        url=row["url"],
        secret=row["secret"],
        events=tuple(row["events"]),
        active=row["active"],
        created_at=row["created_at"],
    )


def _row_to_delivery(row: dict[str, Any]) -> WebhookDelivery:
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return WebhookDelivery(
        delivery_id=row["delivery_id"],
        subscription_id=row["subscription_id"],
        tenant_id=row["tenant_id"],
        trace_id=row["trace_id"],
        event_action=row["event_action"],
        payload=payload or {},
        idempotency_key=row["idempotency_key"],
        status=WebhookDeliveryStatus(row["status"]),
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        dispatched_at=row["dispatched_at"],
    )


class PgWebhookSubscriptionRepository(WebhookSubscriptionRepository):
    """asyncpg-backed subscription persistence."""

    _UPSERT = """
        INSERT INTO webhook_subscriptions (
            subscription_id, tenant_id, url, secret, events, active, created_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (subscription_id) DO UPDATE SET
            url = EXCLUDED.url,
            secret = EXCLUDED.secret,
            events = EXCLUDED.events,
            active = EXCLUDED.active
    """

    # Cross-tenant fetch by id: the dispatcher needs to resolve a
    # delivery's subscription regardless of bound context (it runs in
    # a system loop with no tenant on the contextvar).
    _GET = "SELECT * FROM webhook_subscriptions WHERE subscription_id = $1"

    _LIST_ACTIVE_FOR_EVENT = """
        SELECT * FROM webhook_subscriptions
        WHERE tenant_id = $1 AND active = TRUE AND $2 = ANY(events)
    """

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    async def upsert(self, subscription: WebhookSubscription) -> None:
        check_tenant(subscription.tenant_id)
        async with self._pool.acquire() as conn:
            await conn.execute(
                self._UPSERT,
                subscription.subscription_id,
                subscription.tenant_id,
                subscription.url,
                subscription.secret,
                list(subscription.events),
                subscription.active,
                subscription.created_at,
            )

    async def get(self, subscription_id: str) -> WebhookSubscription | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(self._GET, subscription_id)
        return _row_to_subscription(dict(row)) if row else None

    async def list_active_for_event(
        self,
        tenant_id: str,
        event_action: str,
    ) -> list[WebhookSubscription]:
        check_tenant(tenant_id)
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(self._LIST_ACTIVE_FOR_EVENT, tenant_id, event_action)
        return [_row_to_subscription(dict(r)) for r in rows]


class PgWebhookDeliveryRepository(WebhookDeliveryRepository):
    """asyncpg-backed delivery persistence + dispatcher claim path."""

    # ``ON CONFLICT DO NOTHING`` on ``(tenant_id, idempotency_key)``
    # makes the fanout step naturally idempotent: a redelivered
    # ``audit.task.awaiting_approval`` event tries to enqueue the same
    # delivery and the constraint silently absorbs the duplicate.
    _ENQUEUE = """
        INSERT INTO webhook_deliveries (
            delivery_id, subscription_id, tenant_id, trace_id,
            event_action, payload, idempotency_key, status, attempts,
            next_attempt_at, last_error, created_at, dispatched_at
        )
        VALUES (
            $1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9,
            $10, $11, $12, $13
        )
        ON CONFLICT (tenant_id, idempotency_key) DO NOTHING
    """

    # ``FOR UPDATE SKIP LOCKED`` lets multiple dispatcher instances
    # claim disjoint batches concurrently without blocking on each
    # other; the row remains locked until the outer txn commits.
    _CLAIM = """
        SELECT * FROM webhook_deliveries
        WHERE status = 'pending' AND next_attempt_at <= $1
        ORDER BY next_attempt_at ASC
        LIMIT $2
        FOR UPDATE SKIP LOCKED
    """

    _MARK_DISPATCHED = """
        UPDATE webhook_deliveries
        SET status = 'dispatched',
            dispatched_at = $2,
            attempts = $3,
            last_error = NULL
        WHERE delivery_id = $1
    """

    _MARK_RETRY = """
        UPDATE webhook_deliveries
        SET next_attempt_at = $2,
            last_error = $3,
            attempts = $4
        WHERE delivery_id = $1 AND status = 'pending'
    """

    _MARK_DEAD_LETTER = """
        UPDATE webhook_deliveries
        SET status = 'dead_letter',
            dispatched_at = $2,
            last_error = $3,
            attempts = $4
        WHERE delivery_id = $1
    """

    _GET = "SELECT * FROM webhook_deliveries WHERE delivery_id = $1"

    _COUNT_BY_STATUS = (
        "SELECT COUNT(*) FROM webhook_deliveries WHERE tenant_id = $1 AND status = $2"
    )

    def __init__(self, pool: DatabasePool) -> None:
        self._pool = pool

    async def enqueue(self, delivery: WebhookDelivery) -> None:
        check_tenant(delivery.tenant_id)
        async with self._pool.acquire() as conn:
            await self._enqueue_with_conn(delivery, conn)

    async def enqueue_in_conn(
        self,
        delivery: WebhookDelivery,
        conn: asyncpg.Connection[Any],
    ) -> None:
        """Append on an externally-supplied connection (transactional fanout)."""
        check_tenant(delivery.tenant_id)
        await self._enqueue_with_conn(delivery, conn)

    async def _enqueue_with_conn(
        self,
        delivery: WebhookDelivery,
        conn: asyncpg.Connection[Any],
    ) -> None:
        await conn.execute(
            self._ENQUEUE,
            delivery.delivery_id,
            delivery.subscription_id,
            delivery.tenant_id,
            delivery.trace_id,
            delivery.event_action,
            json.dumps(delivery.payload),
            delivery.idempotency_key,
            delivery.status.value,
            delivery.attempts,
            delivery.next_attempt_at,
            delivery.last_error,
            delivery.created_at,
            delivery.dispatched_at,
        )

    async def claim_pending(
        self,
        *,
        batch_size: int,
        now: datetime,
    ) -> list[WebhookDelivery]:
        # ``transaction()`` holds the row locks for the whole batch
        # life; the dispatcher dispatches inside the same txn so the
        # UPDATE finalising each row is the only thing that releases
        # the lock.
        async with self._pool.transaction() as conn:
            rows = await conn.fetch(self._CLAIM, now, batch_size)
        return [_row_to_delivery(dict(r)) for r in rows]

    async def mark_dispatched(
        self,
        delivery_id: str,
        *,
        dispatched_at: datetime,
        attempts: int,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(self._MARK_DISPATCHED, delivery_id, dispatched_at, attempts)

    async def mark_retry(
        self,
        delivery_id: str,
        *,
        next_attempt_at: datetime,
        last_error: str,
        attempts: int,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(self._MARK_RETRY, delivery_id, next_attempt_at, last_error, attempts)

    async def mark_dead_letter(
        self,
        delivery_id: str,
        *,
        last_error: str,
        attempts: int,
        finalised_at: datetime,
    ) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                self._MARK_DEAD_LETTER,
                delivery_id,
                finalised_at,
                last_error,
                attempts,
            )

    async def get(self, delivery_id: str) -> WebhookDelivery | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(self._GET, delivery_id)
        return _row_to_delivery(dict(row)) if row else None

    async def count_by_status(
        self,
        tenant_id: str,
        status: WebhookDeliveryStatus,
    ) -> int:
        check_tenant(tenant_id)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(self._COUNT_BY_STATUS, tenant_id, status.value)
        return int(row["count"]) if row else 0
