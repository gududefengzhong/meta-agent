"""In-memory fakes for webhook repos shared across unit tests."""

from __future__ import annotations

from datetime import datetime

from meta_agent.core.domain.webhook import (
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookSubscription,
)
from meta_agent.core.ports.webhook import (
    WebhookDeliveryRepository,
    WebhookSubscriptionRepository,
)


class InMemorySubscriptionRepo(WebhookSubscriptionRepository):
    """Cross-tenant in-memory subscription store."""

    def __init__(self) -> None:
        self.rows: dict[str, WebhookSubscription] = {}

    async def upsert(self, subscription: WebhookSubscription) -> None:
        self.rows[subscription.subscription_id] = subscription

    async def get(self, subscription_id: str) -> WebhookSubscription | None:
        return self.rows.get(subscription_id)

    async def list_active_for_event(
        self,
        tenant_id: str,
        event_action: str,
    ) -> list[WebhookSubscription]:
        return [
            s
            for s in self.rows.values()
            if s.tenant_id == tenant_id and s.active and event_action in s.events
        ]


class InMemoryDeliveryRepo(WebhookDeliveryRepository):
    """In-memory delivery store + claim_pending semantics."""

    def __init__(self) -> None:
        self.rows: dict[str, WebhookDelivery] = {}
        # Tracks (tenant_id, idempotency_key) to mimic the SQL unique
        # constraint that absorbs redelivered fanouts.
        self._idem: set[tuple[str, str]] = set()

    async def enqueue(self, delivery: WebhookDelivery) -> None:
        key = (delivery.tenant_id, delivery.idempotency_key)
        if key in self._idem:
            return  # duplicate — no-op, matches SQL ON CONFLICT DO NOTHING
        self.rows[delivery.delivery_id] = delivery
        self._idem.add(key)

    async def claim_pending(
        self,
        *,
        batch_size: int,
        now: datetime,
    ) -> list[WebhookDelivery]:
        candidates = [
            r
            for r in self.rows.values()
            if r.status is WebhookDeliveryStatus.PENDING and r.next_attempt_at <= now
        ]
        candidates.sort(key=lambda r: r.next_attempt_at)
        return candidates[:batch_size]

    async def mark_dispatched(
        self,
        delivery_id: str,
        *,
        dispatched_at: datetime,
        attempts: int,
    ) -> None:
        existing = self.rows.get(delivery_id)
        if existing is None:
            return
        self.rows[delivery_id] = existing.model_copy(
            update={
                "status": WebhookDeliveryStatus.DISPATCHED,
                "dispatched_at": dispatched_at,
                "attempts": attempts,
                "last_error": None,
            }
        )

    async def mark_retry(
        self,
        delivery_id: str,
        *,
        next_attempt_at: datetime,
        last_error: str,
        attempts: int,
    ) -> None:
        existing = self.rows.get(delivery_id)
        if existing is None or existing.status is not WebhookDeliveryStatus.PENDING:
            return
        self.rows[delivery_id] = existing.model_copy(
            update={
                "next_attempt_at": next_attempt_at,
                "last_error": last_error,
                "attempts": attempts,
            }
        )

    async def mark_dead_letter(
        self,
        delivery_id: str,
        *,
        last_error: str,
        attempts: int,
        finalised_at: datetime,
    ) -> None:
        existing = self.rows.get(delivery_id)
        if existing is None:
            return
        self.rows[delivery_id] = existing.model_copy(
            update={
                "status": WebhookDeliveryStatus.DEAD_LETTER,
                "dispatched_at": finalised_at,
                "last_error": last_error,
                "attempts": attempts,
            }
        )

    async def get(self, delivery_id: str) -> WebhookDelivery | None:
        return self.rows.get(delivery_id)

    async def count_by_status(
        self,
        tenant_id: str,
        status: WebhookDeliveryStatus,
    ) -> int:
        return sum(1 for r in self.rows.values() if r.tenant_id == tenant_id and r.status is status)
