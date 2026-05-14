"""Unit tests for :class:`OutboxDispatcher`.

The tests use in-memory fakes for the outbox repository and the
message publisher, so no Postgres or Redis is required. The dispatcher
contract is small enough that the fakes are an honest substitute for
the real adapters.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from meta_agent.core.domain.outbox import OutboxEvent, OutboxStatus
from meta_agent.core.ports.message import MessageEnvelope
from meta_agent.core.ports.queue import MessagePublisher, QueueError
from meta_agent.core.ports.repository import OutboxRepository
from meta_agent.infra.persistence.outbox_dispatcher import (
    DispatcherConfig,
    OutboxDispatcher,
)


class FakePublisher(MessagePublisher):
    def __init__(self, fail_times: int = 0) -> None:
        self.published: list[MessageEnvelope] = []
        self.remaining_failures = fail_times

    async def publish(self, envelope: MessageEnvelope) -> None:
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise QueueError("simulated publish failure")
        self.published.append(envelope)


class FakeOutboxRepo(OutboxRepository):
    def __init__(self) -> None:
        self.rows: dict[str, OutboxEvent] = {}
        self.dispatched: list[tuple[str, datetime]] = []
        self.failed: list[tuple[str, str, datetime | None, bool]] = []

    async def enqueue(self, event: OutboxEvent) -> None:
        self.rows[event.event_id] = event

    async def claim_pending(self, *, batch_size: int, now: datetime) -> list[OutboxEvent]:
        ready = [e for e in self.rows.values() if e.status is OutboxStatus.PENDING]
        return ready[:batch_size]

    async def mark_dispatched(self, event_id: str, *, dispatched_at: datetime) -> None:
        self.dispatched.append((event_id, dispatched_at))
        event = self.rows[event_id]
        self.rows[event_id] = event.model_copy(
            update={"status": OutboxStatus.DISPATCHED, "dispatched_at": dispatched_at}
        )

    async def mark_failed(
        self,
        event_id: str,
        *,
        error: str,
        next_attempt_at: datetime | None,
        terminal: bool,
    ) -> None:
        self.failed.append((event_id, error, next_attempt_at, terminal))
        event = self.rows[event_id]
        self.rows[event_id] = event.model_copy(
            update={
                "status": OutboxStatus.FAILED if terminal else OutboxStatus.PENDING,
                "attempts": event.attempts + 1,
            }
        )

    async def get(self, event_id: str) -> OutboxEvent | None:
        return self.rows.get(event_id)

    async def count_by_status(self, tenant_id: str, status: OutboxStatus) -> int:
        return sum(1 for e in self.rows.values() if e.tenant_id == tenant_id and e.status is status)


def _event(event_id: str, attempts: int = 0) -> OutboxEvent:
    return OutboxEvent(
        event_id=event_id,
        tenant_id="tenant-1",
        trace_id="trace-1",
        aggregate_type="task",
        aggregate_id="task-1",
        topic="task.events",
        payload={"k": "v"},
        idempotency_key=f"idem-{event_id}",
        attempts=attempts,
        created_at=datetime(2026, 5, 14, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_dispatch_marks_row_dispatched_on_success() -> None:
    repo = FakeOutboxRepo()
    await repo.enqueue(_event("e1"))
    publisher = FakePublisher()
    dispatcher = OutboxDispatcher(repo, publisher)

    drained = await dispatcher.run_once()

    assert drained == 1
    assert len(publisher.published) == 1
    assert publisher.published[0].message_id == "e1"
    assert repo.rows["e1"].status is OutboxStatus.DISPATCHED


@pytest.mark.asyncio
async def test_dispatch_reschedules_on_transient_failure() -> None:
    repo = FakeOutboxRepo()
    await repo.enqueue(_event("e1"))
    publisher = FakePublisher(fail_times=1)
    dispatcher = OutboxDispatcher(
        repo,
        publisher,
        config=DispatcherConfig(max_attempts=3, base_backoff=timedelta(seconds=2)),
    )

    await dispatcher.run_once()

    assert publisher.published == []
    assert len(repo.failed) == 1
    event_id, _err, next_at, terminal = repo.failed[0]
    assert event_id == "e1"
    assert terminal is False
    assert next_at is not None
    assert repo.rows["e1"].status is OutboxStatus.PENDING
    assert repo.rows["e1"].attempts == 1


@pytest.mark.asyncio
async def test_dispatch_marks_terminal_after_max_attempts() -> None:
    repo = FakeOutboxRepo()
    await repo.enqueue(_event("e1", attempts=2))  # already at 2
    publisher = FakePublisher(fail_times=1)
    dispatcher = OutboxDispatcher(repo, publisher, config=DispatcherConfig(max_attempts=3))

    await dispatcher.run_once()

    assert len(repo.failed) == 1
    _eid, _err, next_at, terminal = repo.failed[0]
    assert terminal is True
    assert next_at is None
    assert repo.rows["e1"].status is OutboxStatus.FAILED


@pytest.mark.asyncio
async def test_run_once_returns_zero_when_no_pending() -> None:
    repo = FakeOutboxRepo()
    publisher = FakePublisher()
    dispatcher = OutboxDispatcher(repo, publisher)

    assert await dispatcher.run_once() == 0
    assert publisher.published == []
