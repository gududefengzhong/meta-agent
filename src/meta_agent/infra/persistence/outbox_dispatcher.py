"""Outbox dispatcher: relay pending outbox rows to the queue.

The dispatcher implements the publish side of the Transactional Outbox
pattern. Producers persist a row + business state atomically. This
dispatcher periodically claims pending rows (``SELECT ... FOR UPDATE
SKIP LOCKED``), publishes the corresponding :class:`MessageEnvelope`,
and updates the row status. Failures bump ``attempts`` and either
reschedule via exponential backoff or transition the row to terminal
``failed`` after :attr:`max_attempts`.

The dispatcher is intentionally side-effect-only and stateless across
restarts: every replica can claim, publish and ack independently
because the DB row lock provides the cross-replica coordination.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from meta_agent.core.domain.outbox import OutboxEvent
from meta_agent.core.ports.message import MessageEnvelope
from meta_agent.core.ports.queue import MessagePublisher
from meta_agent.core.ports.repository import OutboxRepository
from meta_agent.infra.security.context import RequestContext, bind_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DispatcherConfig:
    """Tuning knobs for :class:`OutboxDispatcher`.

    ``poll_interval`` is the wait between empty polls.
    ``batch_size`` bounds rows claimed per cycle.
    ``max_attempts`` is the per-row retry budget; once exceeded the
    row is marked ``failed`` and not retried.
    ``base_backoff`` / ``max_backoff`` shape the exponential schedule
    (``base * 2**(attempts-1)`` capped by ``max``).
    """

    poll_interval: float = 1.0
    batch_size: int = 32
    max_attempts: int = 8
    base_backoff: timedelta = timedelta(seconds=1)
    max_backoff: timedelta = timedelta(minutes=5)


class OutboxDispatcher:
    """Polls the outbox table and publishes pending rows."""

    def __init__(
        self,
        repository: OutboxRepository,
        publisher: MessagePublisher,
        *,
        config: DispatcherConfig | None = None,
        now: type[datetime] = datetime,
    ) -> None:
        self._repo = repository
        self._publisher = publisher
        self._config = config or DispatcherConfig()
        self._now_cls = now
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False

    async def run_forever(self) -> None:
        """Continuously poll until :meth:`stop` is invoked."""
        if self._running:
            raise RuntimeError("OutboxDispatcher is already running")
        self._running = True
        self._stop_event.clear()
        try:
            while not self._stop_event.is_set():
                drained = await self.run_once()
                if drained == 0:
                    await self._sleep_or_stop(self._config.poll_interval)
        finally:
            self._running = False

    async def stop(self) -> None:
        self._stop_event.set()

    async def run_once(self) -> int:
        """Drain one batch of pending rows. Returns rows processed."""
        now = self._now()
        events = await self._repo.claim_pending(batch_size=self._config.batch_size, now=now)
        for event in events:
            await self._dispatch_one(event)
        return len(events)

    async def _dispatch_one(self, event: OutboxEvent) -> None:
        ctx = _event_to_context(event)
        envelope = _event_to_envelope(event, enqueued_at=self._now())
        try:
            with bind_context(ctx):
                await self._publisher.publish(envelope)
        except Exception as exc:
            await self._handle_failure(event, exc)
            return
        await self._repo.mark_dispatched(event.event_id, dispatched_at=self._now())

    async def _handle_failure(self, event: OutboxEvent, exc: BaseException) -> None:
        next_attempt = event.attempts + 1
        terminal = next_attempt >= self._config.max_attempts
        next_at: datetime | None
        if terminal:
            next_at = None
        else:
            delay = min(
                self._config.base_backoff * (2**event.attempts),
                self._config.max_backoff,
            )
            next_at = self._now() + delay
        logger.warning(
            "outbox.dispatch_failed",
            extra={
                "event_id": event.event_id,
                "attempts": next_attempt,
                "terminal": terminal,
                "error": str(exc),
            },
        )
        await self._repo.mark_failed(
            event.event_id,
            error=str(exc),
            next_attempt_at=next_at,
            terminal=terminal,
        )

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except TimeoutError:
            return

    def _now(self) -> datetime:
        return self._now_cls.now(UTC)


def _event_to_envelope(event: OutboxEvent, *, enqueued_at: datetime) -> MessageEnvelope:
    # When the row carries a task aggregate, surface ``aggregate_id``
    # as ``task_id`` too so worker dispatch (which keys off
    # ``envelope.task_id``) sees the same shape it gets from any
    # producer that publishes envelopes directly.
    task_id = event.aggregate_id if event.aggregate_type == "task" else None
    return MessageEnvelope(
        message_id=event.event_id,
        topic=event.topic,
        tenant_id=event.tenant_id,
        trace_id=event.trace_id,
        idempotency_key=event.idempotency_key,
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        task_id=task_id,
        event_type=event.aggregate_type,
        payload=event.payload,
        attempts=event.attempts,
        occurred_at=event.created_at,
        enqueued_at=enqueued_at,
    )


def _event_to_context(event: OutboxEvent) -> RequestContext:
    return RequestContext(
        tenant_id=event.tenant_id,
        principal_id="system",
        trace_id=event.trace_id,
        request_id=event.event_id,
        idempotency_key=event.idempotency_key,
    )
