"""Webhook subscription + delivery ports (Phase γ-B-2).

Two independent repositories rather than one:

* :class:`WebhookSubscriptionRepository` — operator-supplied config,
  read by the fanout step at audit-emit time.
* :class:`WebhookDeliveryRepository` — append-only per-attempt
  records, written by fanout and consumed by the dispatcher.

Splitting them keeps the dispatcher's hot query
(``claim_pending``) free of any join against the subscription table
beyond what the dispatcher actually needs (URL + secret).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from meta_agent.core.domain.webhook import (
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookSubscription,
)


class WebhookSubscriptionRepository(ABC):
    """Read / write access to per-tenant webhook subscriptions."""

    @abstractmethod
    async def upsert(self, subscription: WebhookSubscription) -> None:
        """Insert or update a subscription row (idempotent on ``subscription_id``)."""

    @abstractmethod
    async def get(self, subscription_id: str) -> WebhookSubscription | None:
        """Cross-tenant fetch by id. Used by the dispatcher to resolve url + secret."""

    @abstractmethod
    async def list_active_for_event(
        self,
        tenant_id: str,
        event_action: str,
    ) -> list[WebhookSubscription]:
        """Return active subscriptions in ``tenant_id`` that listen to ``event_action``.

        Used by the fanout step at audit-emit time to decide which
        subscriptions get a delivery row.
        """


class WebhookDeliveryRepository(ABC):
    """Append-only persistence + dispatcher claim surface for deliveries."""

    @abstractmethod
    async def enqueue(self, delivery: WebhookDelivery) -> None:
        """Persist a new ``PENDING`` delivery row.

        Idempotent on ``idempotency_key`` (``tenant_id``-scoped) so a
        redelivered audit fanout does not create duplicate deliveries.
        """

    @abstractmethod
    async def claim_pending(
        self,
        *,
        batch_size: int,
        now: datetime,
    ) -> list[WebhookDelivery]:
        """Atomically claim a batch of due ``PENDING`` rows for dispatch.

        "Due" means ``status='pending' AND next_attempt_at <= now``.
        The implementation is responsible for ensuring two dispatcher
        instances do not claim the same row (e.g. via ``FOR UPDATE
        SKIP LOCKED``).
        """

    @abstractmethod
    async def mark_dispatched(
        self,
        delivery_id: str,
        *,
        dispatched_at: datetime,
        attempts: int,
    ) -> None:
        """Terminal success transition."""

    @abstractmethod
    async def mark_retry(
        self,
        delivery_id: str,
        *,
        next_attempt_at: datetime,
        last_error: str,
        attempts: int,
    ) -> None:
        """Schedule the next attempt and record the most recent failure."""

    @abstractmethod
    async def mark_dead_letter(
        self,
        delivery_id: str,
        *,
        last_error: str,
        attempts: int,
        finalised_at: datetime,
    ) -> None:
        """Terminal failure transition after the retry budget is exhausted."""

    @abstractmethod
    async def get(self, delivery_id: str) -> WebhookDelivery | None:
        """Fetch one delivery; used by tests + introspection paths."""

    @abstractmethod
    async def count_by_status(
        self,
        tenant_id: str,
        status: WebhookDeliveryStatus,
    ) -> int:
        """Per-tenant count for ops dashboards."""
