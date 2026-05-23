"""WebhookDispatcher — drain ``webhook_deliveries`` into HTTP POSTs.

The dispatcher is a long-running loop, structurally identical to the
existing :class:`OutboxDispatcher`: ``run_once`` claims a batch of
pending rows, attempts to deliver each, and updates the row's
lifecycle. Two dispatcher instances can run concurrently without
double-delivery because the claim query uses ``FOR UPDATE SKIP
LOCKED`` at the SQL layer.

Retry policy:

* On 2xx response → mark ``DISPATCHED``.
* On 3xx/4xx/5xx response, timeout, or network error → bump
  ``attempts``; if under the cap, schedule the next attempt with
  exponential backoff; otherwise terminate as ``DEAD_LETTER``.
* HMAC signature is computed over the JSON-encoded payload with the
  subscription's secret and sent in ``X-Meta-Agent-Signature``.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from meta_agent.core.domain.webhook import WebhookDelivery
from meta_agent.core.ports.webhook import (
    WebhookDeliveryRepository,
    WebhookSubscriptionRepository,
)
from meta_agent.infra.webhook.signing import SIGNATURE_HEADER, compute_signature

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WebhookDispatcherConfig:
    """Tuning knobs for :class:`WebhookDispatcher`."""

    batch_size: int = 32
    max_attempts: int = 8
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 6 * 3600.0  # cap at 6h between attempts
    request_timeout_seconds: float = 10.0


def compute_next_attempt_at(
    *,
    now: datetime,
    attempt_number: int,
    config: WebhookDispatcherConfig,
) -> datetime:
    """Exponential backoff with a hard cap.

    ``attempt_number`` is the count of failed attempts so far (1 for
    the row we just failed for the first time). The delay is
    ``base * 2 ** (attempt_number - 1)`` clamped to
    ``max_delay_seconds``; the cap matters once attempts grow past
    ~10 so the schedule does not run away.
    """

    if attempt_number < 1:
        raise ValueError("attempt_number must be >= 1")
    raw = config.base_delay_seconds * (2 ** (attempt_number - 1))
    delay = min(raw, config.max_delay_seconds)
    # ``math.isfinite`` defends against silly configs (delay=inf via
    # huge attempt counts on floats); we clamp explicitly above so
    # this is belt-and-braces.
    if not math.isfinite(delay):
        delay = config.max_delay_seconds
    return now + timedelta(seconds=delay)


class WebhookDispatcher:
    """Drains ``webhook_deliveries`` into HTTP POSTs with HMAC signing."""

    def __init__(
        self,
        *,
        deliveries: WebhookDeliveryRepository,
        subscriptions: WebhookSubscriptionRepository,
        http_client: httpx.AsyncClient,
        config: WebhookDispatcherConfig | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._deliveries = deliveries
        self._subscriptions = subscriptions
        self._http = http_client
        self._config = config or WebhookDispatcherConfig()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run_once(self) -> int:
        """Process one batch. Returns the number of deliveries handled."""

        now = self._clock()
        batch = await self._deliveries.claim_pending(
            batch_size=self._config.batch_size,
            now=now,
        )
        for delivery in batch:
            await self._dispatch(delivery)
        return len(batch)

    async def _dispatch(self, delivery: WebhookDelivery) -> None:
        sub = await self._subscriptions.get(delivery.subscription_id)
        attempts = delivery.attempts + 1
        if sub is None or not sub.active:
            # Subscription deactivated after the delivery was enqueued.
            # Terminate as dead-letter so the dispatcher does not spin
            # on it forever.
            await self._deliveries.mark_dead_letter(
                delivery.delivery_id,
                last_error="subscription missing or inactive",
                attempts=attempts,
                finalised_at=self._clock(),
            )
            return
        body = json.dumps(delivery.payload, sort_keys=True).encode("utf-8")
        signature = compute_signature(sub.secret, body)
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: signature,
            "X-Meta-Agent-Event": delivery.event_action,
            "X-Meta-Agent-Idempotency": delivery.idempotency_key,
            "X-Meta-Agent-Trace-Id": delivery.trace_id,
        }
        try:
            response = await self._http.post(
                sub.url,
                content=body,
                headers=headers,
                timeout=self._config.request_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            await self._handle_failure(delivery, attempts, f"timeout: {exc!s}")
            return
        except httpx.HTTPError as exc:
            await self._handle_failure(delivery, attempts, f"network: {exc!s}")
            return
        if 200 <= response.status_code < 300:
            await self._deliveries.mark_dispatched(
                delivery.delivery_id,
                dispatched_at=self._clock(),
                attempts=attempts,
            )
            return
        await self._handle_failure(
            delivery,
            attempts,
            f"http {response.status_code}",
        )

    async def _handle_failure(
        self,
        delivery: WebhookDelivery,
        attempts: int,
        last_error: str,
    ) -> None:
        if attempts >= self._config.max_attempts:
            await self._deliveries.mark_dead_letter(
                delivery.delivery_id,
                last_error=last_error,
                attempts=attempts,
                finalised_at=self._clock(),
            )
            return
        next_at = compute_next_attempt_at(
            now=self._clock(),
            attempt_number=attempts,
            config=self._config,
        )
        await self._deliveries.mark_retry(
            delivery.delivery_id,
            next_attempt_at=next_at,
            last_error=last_error,
            attempts=attempts,
        )
