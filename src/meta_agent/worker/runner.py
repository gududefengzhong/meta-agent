"""Worker loop that executes orchestration graphs."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.checkpoint import TaskCheckpoint
from meta_agent.core.domain.task import Task, TaskState
from meta_agent.core.orchestration import Graph, GraphRegistry, TaskRunState
from meta_agent.core.ports.message import MessageEnvelope
from meta_agent.core.ports.repository import (
    AuditRepository,
    CheckpointRepository,
    TaskRepository,
)
from meta_agent.infra.queue.redis_consumer import DeliveredMessage
from meta_agent.infra.security.context import RequestContext, bind_context

logger = logging.getLogger(__name__)


class DeliveryStream(Protocol):
    """Pull-based queue surface the worker depends on.

    Implemented by :class:`meta_agent.infra.queue.RedisStreamConsumer`;
    tests substitute an in-memory fake.
    """

    async def claim_batch(
        self,
        *,
        block_ms: int | None = None,
    ) -> list[DeliveredMessage]: ...

    async def ack(self, entry_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    """Tuning knobs for :class:`WorkerLoop`.

    ``max_attempts`` bounds redelivery: a message whose PEL delivery
    count exceeds this value is treated as abandoned. ``block_ms``
    controls how long ``claim_batch`` may wait for new messages.
    """

    max_attempts: int = 3
    block_ms: int = 1_000


class WorkerLoop:
    """Pulls messages from a :class:`DeliveryStream` and drives graphs."""

    def __init__(
        self,
        *,
        stream: DeliveryStream,
        tasks: TaskRepository,
        checkpoints: CheckpointRepository,
        audits: AuditRepository,
        registry: GraphRegistry,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        config: WorkerConfig | None = None,
    ) -> None:
        self._stream = stream
        self._tasks = tasks
        self._checkpoints = checkpoints
        self._audits = audits
        self._registry = registry
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self._config = config or WorkerConfig()
        self._stop_event: asyncio.Event = asyncio.Event()
        self._running: bool = False

    async def run_once(self) -> int:
        """Process one batch. Returns the number of messages handled."""

        batch = await self._stream.claim_batch(block_ms=self._config.block_ms)
        for msg in batch:
            await self._handle(msg)
        return len(batch)

    async def run_forever(self) -> None:
        if self._running:
            raise RuntimeError("WorkerLoop is already running")
        self._running = True
        self._stop_event.clear()
        try:
            while not self._stop_event.is_set():
                await self.run_once()
        finally:
            self._running = False

    async def stop(self) -> None:
        self._stop_event.set()

    async def _handle(self, msg: DeliveredMessage) -> None:
        envelope = msg.envelope
        ctx = _envelope_to_context(envelope)
        with bind_context(ctx):
            if msg.delivery_count > self._config.max_attempts:
                await self._abandon(msg, ctx, reason="delivery_count_exceeded")
                return
            try:
                await self._dispatch(msg, ctx)
            except Exception:
                logger.exception(
                    "worker.handler_failed",
                    extra={
                        "task_id": envelope.task_id,
                        "entry_id": msg.entry_id,
                        "delivery_count": msg.delivery_count,
                    },
                )
                # leave in PEL; redelivery will increment delivery_count

    async def _dispatch(self, msg: DeliveredMessage, ctx: RequestContext) -> None:
        envelope = msg.envelope
        if envelope.task_id is None:
            await self._audit(ctx, "worker.envelope_invalid", payload={"reason": "missing_task_id"})
            await self._stream.ack(msg.entry_id)
            return
        task = await self._tasks.get(envelope.tenant_id, envelope.task_id)
        if task is None:
            await self._audit(ctx, "worker.task_missing", payload={"task_id": envelope.task_id})
            await self._stream.ack(msg.entry_id)
            return
        graph = self._registry.resolve(task.task_type, task.graph_id)
        state = await self._load_state(task, graph)
        if task.state != TaskState.RUNNING:
            await self._tasks.update_state(
                task.tenant_id, task.task_id, TaskState.RUNNING, self._clock()
            )
        while not state.finished:
            state = await graph.step(state)
            await self._persist_step(task, state)
        final = TaskState.FAILED if state.error else TaskState.SUCCEEDED
        await self._tasks.update_state(task.tenant_id, task.task_id, final, self._clock())
        await self._audit(
            ctx,
            f"task.{final.value}",
            payload={"sequence": state.sequence, "output": state.data.get("output")},
        )
        await self._stream.ack(msg.entry_id)

    async def _load_state(self, task: Task, graph: Graph) -> TaskRunState:
        latest = await self._checkpoints.latest(task.tenant_id, task.task_id)
        if latest is None:
            return TaskRunState(
                task_id=task.task_id,
                tenant_id=task.tenant_id,
                trace_id=task.trace_id,
                graph_id=graph.graph_id,
                data=dict(task.input_payload),
            )
        return TaskRunState.model_validate(latest.state_snapshot)

    async def _persist_step(self, task: Task, state: TaskRunState) -> None:
        cp = TaskCheckpoint(
            checkpoint_id=self._id_factory(),
            task_id=task.task_id,
            tenant_id=task.tenant_id,
            trace_id=task.trace_id,
            node_name=state.current_node,
            sequence=state.sequence,
            state_snapshot=state.model_dump(mode="json"),
            created_at=self._clock(),
        )
        await self._checkpoints.append(cp)
        await self._audit(
            _task_to_context(task),
            "task.node_completed",
            payload={"node": state.current_node, "sequence": state.sequence},
        )

    async def _abandon(self, msg: DeliveredMessage, ctx: RequestContext, *, reason: str) -> None:
        envelope = msg.envelope
        await self._audit(
            ctx,
            "task.abandoned",
            payload={"delivery_count": msg.delivery_count, "reason": reason},
        )
        if envelope.task_id is not None:
            await self._tasks.update_state(
                envelope.tenant_id, envelope.task_id, TaskState.FAILED, self._clock()
            )
        await self._stream.ack(msg.entry_id)

    async def _audit(
        self,
        ctx: RequestContext,
        action: str,
        *,
        payload: dict[str, object],
    ) -> None:
        event = AuditEvent(
            event_id=self._id_factory(),
            tenant_id=ctx.tenant_id,
            principal_id=ctx.principal_id,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            trace_id=ctx.trace_id,
            action=action,
            payload=payload,
            occurred_at=self._clock(),
        )
        await self._audits.append(event)


def _envelope_to_context(envelope: MessageEnvelope) -> RequestContext:
    return RequestContext(
        tenant_id=envelope.tenant_id,
        principal_id=envelope.principal_id or "system",
        trace_id=envelope.trace_id,
        request_id=envelope.request_id or envelope.message_id,
        session_id=envelope.session_id,
        task_id=envelope.task_id,
        idempotency_key=envelope.idempotency_key,
    )


def _task_to_context(task: Task) -> RequestContext:
    return RequestContext(
        tenant_id=task.tenant_id,
        principal_id=task.principal_id,
        trace_id=task.trace_id,
        request_id=task.task_id,
        session_id=task.session_id,
        task_id=task.task_id,
        idempotency_key=task.idempotency_key,
    )
