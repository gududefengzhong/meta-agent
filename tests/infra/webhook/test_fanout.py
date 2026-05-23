"""Unit tests for :class:`WebhookFanout`."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.webhook import (
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookSubscription,
)
from meta_agent.infra.webhook.fanout import WebhookFanout
from tests.infra.webhook._fakes import InMemoryDeliveryRepo, InMemorySubscriptionRepo


def _fixed_clock() -> Callable[[], datetime]:
    return lambda: datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)


def _id_factory() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"id-{next(counter)}"


def _audit(
    *,
    action: str = "task.awaiting_approval",
    tenant_id: str = "t-1",
    payload: dict[str, object] | None = None,
    event_id: str = "ev-1",
) -> AuditEvent:
    return AuditEvent(
        event_id=event_id,
        tenant_id=tenant_id,
        principal_id="user-1",
        session_id=None,
        task_id="task-1",
        trace_id="trace-1",
        action=action,
        payload=payload or {"gate_id": "before_push"},
        occurred_at=datetime(2026, 5, 23, 11, 59, 0, tzinfo=UTC),
    )


def _sub(
    *,
    subscription_id: str = "sub-1",
    tenant_id: str = "t-1",
    events: tuple[str, ...] = ("task.awaiting_approval",),
    active: bool = True,
    url: str = "https://example.test/hook",
) -> WebhookSubscription:
    return WebhookSubscription(
        subscription_id=subscription_id,
        tenant_id=tenant_id,
        url=url,
        secret="shared-secret-32chars-long-x",
        events=events,
        active=active,
        created_at=datetime(2026, 5, 23, tzinfo=UTC),
    )


def _build_fanout(
    *,
    subs: InMemorySubscriptionRepo,
    delivs: InMemoryDeliveryRepo,
    watched: frozenset[str] = frozenset({"task.awaiting_approval"}),
) -> WebhookFanout:
    return WebhookFanout(
        subscriptions=subs,
        deliveries=delivs,
        watched_actions=watched,
        clock=_fixed_clock(),
        id_factory=_id_factory(),
    )


async def test_fanout_ignores_unwatched_actions() -> None:
    subs = InMemorySubscriptionRepo()
    delivs = InMemoryDeliveryRepo()
    await subs.upsert(_sub())
    fanout = _build_fanout(subs=subs, delivs=delivs)
    wrote = await fanout.fanout(_audit(action="task.succeeded"))
    assert wrote == 0
    assert delivs.rows == {}


async def test_fanout_enqueues_one_delivery_per_matching_subscription() -> None:
    subs = InMemorySubscriptionRepo()
    delivs = InMemoryDeliveryRepo()
    await subs.upsert(_sub(subscription_id="sub-a"))
    await subs.upsert(_sub(subscription_id="sub-b"))
    # Mismatched event filter → must not be notified.
    await subs.upsert(_sub(subscription_id="sub-other-event", events=("task.succeeded",)))
    # Different tenant → must not leak across tenants.
    await subs.upsert(_sub(subscription_id="sub-other-tenant", tenant_id="t-other"))
    # Deactivated → must not be notified.
    await subs.upsert(_sub(subscription_id="sub-inactive", active=False))

    fanout = _build_fanout(subs=subs, delivs=delivs)
    wrote = await fanout.fanout(_audit())

    assert wrote == 2
    triggered = {r.subscription_id for r in delivs.rows.values()}
    assert triggered == {"sub-a", "sub-b"}
    for delivery in delivs.rows.values():
        assert delivery.status is WebhookDeliveryStatus.PENDING
        assert delivery.next_attempt_at == datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)
        assert delivery.attempts == 0
        # Payload carries the full audit projection so the subscriber
        # has everything it needs without a follow-up fetch.
        assert delivery.payload["action"] == "task.awaiting_approval"
        assert delivery.payload["task_id"] == "task-1"
        assert delivery.payload["payload"] == {"gate_id": "before_push"}


async def test_fanout_idempotency_key_collapses_redelivered_fanouts() -> None:
    subs = InMemorySubscriptionRepo()
    delivs = InMemoryDeliveryRepo()
    await subs.upsert(_sub())
    fanout = _build_fanout(subs=subs, delivs=delivs)

    first = await fanout.fanout(_audit(event_id="ev-X"))
    second = await fanout.fanout(_audit(event_id="ev-X"))

    # Both calls report 1 (one matching subscription) because the
    # repo's ``enqueue`` is silent about whether the row was actually
    # inserted. The DB unique-key constraint absorbs the duplicate;
    # only one row exists in the store.
    assert first == 1
    assert second == 1
    assert len(delivs.rows) == 1


async def test_fanout_swallows_repo_failures_and_counts_only_writes() -> None:
    """A subscription whose enqueue fails must not block other subscriptions."""

    subs = InMemorySubscriptionRepo()
    await subs.upsert(_sub(subscription_id="sub-a"))
    await subs.upsert(_sub(subscription_id="sub-b"))

    class _FlakyDelivs(InMemoryDeliveryRepo):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        async def enqueue(self, delivery: WebhookDelivery) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("simulated transient")
            await super().enqueue(delivery)

    delivs = _FlakyDelivs()
    fanout = _build_fanout(subs=subs, delivs=delivs)
    wrote = await fanout.fanout(_audit())

    assert wrote == 1
    assert len(delivs.rows) == 1


async def test_fanout_rejects_empty_watched_actions_constructor() -> None:
    with pytest.raises(ValueError, match="watched_actions"):
        WebhookFanout(
            subscriptions=InMemorySubscriptionRepo(),
            deliveries=InMemoryDeliveryRepo(),
            watched_actions=frozenset(),
        )
