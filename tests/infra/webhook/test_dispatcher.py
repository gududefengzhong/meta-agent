"""Unit tests for :class:`WebhookDispatcher` + backoff calculator."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from meta_agent.core.domain.webhook import (
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookSubscription,
)
from meta_agent.infra.webhook.dispatcher import (
    WebhookDispatcher,
    WebhookDispatcherConfig,
    compute_next_attempt_at,
)
from meta_agent.infra.webhook.signing import SIGNATURE_HEADER, verify_signature
from tests.infra.webhook._fakes import InMemoryDeliveryRepo, InMemorySubscriptionRepo

NOW = datetime(2026, 5, 23, 12, 0, 0, tzinfo=UTC)


def _fixed_clock(t: datetime = NOW) -> Callable[[], datetime]:
    return lambda: t


def _sub(
    *,
    subscription_id: str = "sub-1",
    active: bool = True,
    url: str = "https://example.test/hook",
) -> WebhookSubscription:
    return WebhookSubscription(
        subscription_id=subscription_id,
        tenant_id="t-1",
        url=url,
        secret="hex-secret",
        events=("task.awaiting_approval",),
        active=active,
        created_at=NOW,
    )


def _delivery(
    *,
    delivery_id: str = "d-1",
    subscription_id: str = "sub-1",
    attempts: int = 0,
    next_attempt_at: datetime = NOW,
) -> WebhookDelivery:
    return WebhookDelivery(
        delivery_id=delivery_id,
        subscription_id=subscription_id,
        tenant_id="t-1",
        trace_id="trace-1",
        event_action="task.awaiting_approval",
        payload={"task_id": "task-1", "action": "task.awaiting_approval"},
        idempotency_key=f"audit:ev-1:{subscription_id}",
        status=WebhookDeliveryStatus.PENDING,
        attempts=attempts,
        next_attempt_at=next_attempt_at,
        last_error=None,
        created_at=NOW,
        dispatched_at=None,
    )


# ---------------------------------------------------------------------------
# Backoff
# ---------------------------------------------------------------------------


def test_backoff_grows_exponentially_with_base_seconds() -> None:
    cfg = WebhookDispatcherConfig(base_delay_seconds=1.0, max_delay_seconds=10_000.0)
    deltas = [
        (compute_next_attempt_at(now=NOW, attempt_number=n, config=cfg) - NOW).total_seconds()
        for n in (1, 2, 3, 4)
    ]
    assert deltas == [1.0, 2.0, 4.0, 8.0]


def test_backoff_clamps_to_max_delay() -> None:
    cfg = WebhookDispatcherConfig(base_delay_seconds=1.0, max_delay_seconds=5.0)
    far_future = compute_next_attempt_at(now=NOW, attempt_number=20, config=cfg)
    assert (far_future - NOW).total_seconds() == 5.0


def test_backoff_rejects_attempt_number_below_one() -> None:
    cfg = WebhookDispatcherConfig()
    with pytest.raises(ValueError, match="attempt_number"):
        compute_next_attempt_at(now=NOW, attempt_number=0, config=cfg)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


async def _build_dispatcher(
    *,
    subs: InMemorySubscriptionRepo,
    delivs: InMemoryDeliveryRepo,
    handler: Callable[[httpx.Request], httpx.Response],
    config: WebhookDispatcherConfig | None = None,
) -> WebhookDispatcher:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return WebhookDispatcher(
        deliveries=delivs,
        subscriptions=subs,
        http_client=client,
        config=config or WebhookDispatcherConfig(base_delay_seconds=1.0, max_attempts=3),
        clock=_fixed_clock(),
    )


async def test_dispatch_2xx_marks_dispatched_and_signs_body() -> None:
    subs = InMemorySubscriptionRepo()
    delivs = InMemoryDeliveryRepo()
    await subs.upsert(_sub())
    await delivs.enqueue(_delivery())

    received: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        received["body"] = request.content.decode()
        received["signature"] = request.headers[SIGNATURE_HEADER]
        received["event"] = request.headers["X-Meta-Agent-Event"]
        return httpx.Response(204)

    dispatcher = await _build_dispatcher(subs=subs, delivs=delivs, handler=handler)
    count = await dispatcher.run_once()

    assert count == 1
    stored = await delivs.get("d-1")
    assert stored is not None
    assert stored.status is WebhookDeliveryStatus.DISPATCHED
    assert stored.attempts == 1
    assert stored.dispatched_at == NOW
    # The HMAC computed by the dispatcher must round-trip through the
    # public verify helper using the same secret.
    assert verify_signature("hex-secret", received["body"].encode("utf-8"), received["signature"])
    assert received["event"] == "task.awaiting_approval"


async def test_dispatch_non_2xx_schedules_retry_with_backoff() -> None:
    subs = InMemorySubscriptionRepo()
    delivs = InMemoryDeliveryRepo()
    await subs.upsert(_sub())
    await delivs.enqueue(_delivery())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    dispatcher = await _build_dispatcher(subs=subs, delivs=delivs, handler=handler)
    await dispatcher.run_once()

    stored = await delivs.get("d-1")
    assert stored is not None
    assert stored.status is WebhookDeliveryStatus.PENDING
    assert stored.attempts == 1
    assert stored.last_error == "http 503"
    # First retry: base_delay=1s → next_attempt_at = NOW + 1s.
    assert stored.next_attempt_at == NOW + timedelta(seconds=1.0)


async def test_dispatch_network_error_schedules_retry() -> None:
    subs = InMemorySubscriptionRepo()
    delivs = InMemoryDeliveryRepo()
    await subs.upsert(_sub())
    await delivs.enqueue(_delivery())

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    dispatcher = await _build_dispatcher(subs=subs, delivs=delivs, handler=handler)
    await dispatcher.run_once()

    stored = await delivs.get("d-1")
    assert stored is not None
    assert stored.status is WebhookDeliveryStatus.PENDING
    assert stored.last_error is not None
    assert "network" in stored.last_error


async def test_dispatch_timeout_schedules_retry() -> None:
    subs = InMemorySubscriptionRepo()
    delivs = InMemoryDeliveryRepo()
    await subs.upsert(_sub())
    await delivs.enqueue(_delivery())

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    dispatcher = await _build_dispatcher(subs=subs, delivs=delivs, handler=handler)
    await dispatcher.run_once()

    stored = await delivs.get("d-1")
    assert stored is not None
    assert stored.last_error is not None
    assert "timeout" in stored.last_error


async def test_dispatch_dead_letters_after_max_attempts() -> None:
    subs = InMemorySubscriptionRepo()
    delivs = InMemoryDeliveryRepo()
    await subs.upsert(_sub())
    # Pre-fail the row to ``max_attempts - 1`` so the next failure is
    # terminal.
    await delivs.enqueue(_delivery(attempts=2))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    dispatcher = await _build_dispatcher(
        subs=subs,
        delivs=delivs,
        handler=handler,
        config=WebhookDispatcherConfig(max_attempts=3, base_delay_seconds=1.0),
    )
    await dispatcher.run_once()

    stored = await delivs.get("d-1")
    assert stored is not None
    assert stored.status is WebhookDeliveryStatus.DEAD_LETTER
    assert stored.attempts == 3
    assert stored.last_error == "http 500"


async def test_dispatch_dead_letters_when_subscription_inactive() -> None:
    subs = InMemorySubscriptionRepo()
    delivs = InMemoryDeliveryRepo()
    await subs.upsert(_sub(active=False))
    await delivs.enqueue(_delivery())

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("HTTP call should not have fired")

    dispatcher = await _build_dispatcher(subs=subs, delivs=delivs, handler=handler)
    await dispatcher.run_once()

    stored = await delivs.get("d-1")
    assert stored is not None
    assert stored.status is WebhookDeliveryStatus.DEAD_LETTER
    assert stored.last_error == "subscription missing or inactive"


async def test_dispatch_skips_deliveries_not_yet_due() -> None:
    subs = InMemorySubscriptionRepo()
    delivs = InMemoryDeliveryRepo()
    await subs.upsert(_sub())
    # Schedule a delivery for 5 minutes from now → claim_pending must
    # not return it under the fixed clock.
    await delivs.enqueue(_delivery(next_attempt_at=NOW + timedelta(minutes=5)))

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("HTTP call should not have fired")

    dispatcher = await _build_dispatcher(subs=subs, delivs=delivs, handler=handler)
    count = await dispatcher.run_once()
    assert count == 0
    stored = await delivs.get("d-1")
    assert stored is not None
    assert stored.status is WebhookDeliveryStatus.PENDING
