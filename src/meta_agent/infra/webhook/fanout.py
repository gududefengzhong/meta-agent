"""WebhookFanout — convert one audit emission into N delivery rows.

The fanout is the bridge from the existing ``AuditSink.append`` path
(worker / approval gateway / future producers) to the new
``webhook_deliveries`` queue. For each audit event whose ``action``
matches a subscription's ``events`` filter, we write one delivery row
with the same trace_id + tenant_id + a payload carved from the audit
row.

Design constraints:

* **Best-effort**: the fanout MUST NOT raise into the caller; if the
  subscription / delivery write blows up, the audit emission has
  already landed and the operator's visibility into the audit table
  is the recovery path. A future PR can add a "stuck audits"
  sweeper.
* **Idempotent**: the delivery's ``idempotency_key`` is derived
  deterministically from ``(audit_event_id, subscription_id)`` so a
  redelivered audit fanout (caller retried, worker restarted between
  audit-write and fanout) collapses to a no-op via the DB unique
  constraint.
* **Allow-list of actions**: only audit actions that subscriptions
  actually care about turn into deliveries. The fanout is called for
  every audit row but most calls short-circuit before any DB read.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.webhook import WebhookDelivery, WebhookDeliveryStatus
from meta_agent.core.ports.webhook import (
    WebhookDeliveryRepository,
    WebhookSubscriptionRepository,
)

logger = logging.getLogger(__name__)


class WebhookFanout:
    """Audit-event → ``webhook_deliveries`` row fanout, best-effort."""

    def __init__(
        self,
        *,
        subscriptions: WebhookSubscriptionRepository,
        deliveries: WebhookDeliveryRepository,
        watched_actions: frozenset[str],
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not watched_actions:
            raise ValueError(
                "WebhookFanout: watched_actions must list at least one audit action; "
                "use Phase γ-A's `task.awaiting_approval` as the conservative default"
            )
        self._subscriptions = subscriptions
        self._deliveries = deliveries
        self._watched = watched_actions
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))

    async def fanout(self, event: AuditEvent) -> int:
        """Enqueue one delivery row per matching active subscription.

        Returns the number of subscriptions for which an enqueue was
        successfully attempted — duplicates absorbed by the DB unique
        constraint are still counted (the repo is silent on whether
        the row was actually inserted). Zero when no subscription
        listens to ``event.action`` or every attempt raised. Never
        raises into the caller.
        """

        if event.action not in self._watched:
            return 0
        try:
            subs = await self._subscriptions.list_active_for_event(event.tenant_id, event.action)
        except Exception:
            logger.exception(
                "webhook.fanout.lookup_failed",
                extra={
                    "tenant_id": event.tenant_id,
                    "action": event.action,
                    "event_id": event.event_id,
                },
            )
            return 0
        if not subs:
            return 0
        now = self._clock()
        wrote = 0
        for sub in subs:
            delivery = WebhookDelivery(
                delivery_id=self._id_factory(),
                subscription_id=sub.subscription_id,
                tenant_id=event.tenant_id,
                trace_id=event.trace_id,
                event_action=event.action,
                payload=_payload_from(event),
                # Deterministic key collapses redelivered fanouts to a no-op
                # via the `(tenant_id, idempotency_key)` unique constraint.
                idempotency_key=f"audit:{event.event_id}:{sub.subscription_id}",
                status=WebhookDeliveryStatus.PENDING,
                attempts=0,
                next_attempt_at=now,
                last_error=None,
                created_at=now,
                dispatched_at=None,
            )
            try:
                await self._deliveries.enqueue(delivery)
                wrote += 1
            except Exception:
                logger.exception(
                    "webhook.fanout.enqueue_failed",
                    extra={
                        "tenant_id": event.tenant_id,
                        "subscription_id": sub.subscription_id,
                        "event_id": event.event_id,
                    },
                )
        return wrote


def _payload_from(event: AuditEvent) -> dict[str, Any]:
    """Carve a JSON-safe payload from the audit row for delivery body."""

    return {
        "event_id": event.event_id,
        "tenant_id": event.tenant_id,
        "trace_id": event.trace_id,
        "task_id": event.task_id,
        "session_id": event.session_id,
        "action": event.action,
        "payload": dict(event.payload),
        "occurred_at": event.occurred_at.isoformat(),
    }
