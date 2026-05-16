"""Worker loop that executes orchestration graphs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.checkpoint import TaskCheckpoint
from meta_agent.core.domain.task import Task, TaskState
from meta_agent.core.domain.workspace import Workspace
from meta_agent.core.orchestration import Graph, GraphRegistry, TaskRunState
from meta_agent.core.orchestration.result import (
    TaskError,
    TaskErrorCode,
    TaskResult,
    TaskResultStatus,
)
from meta_agent.core.ports.message import MessageEnvelope
from meta_agent.core.ports.repository import (
    TERMINAL_TASK_STATES,
    AuditRepository,
    CheckpointRepository,
    IllegalTaskTransitionError,
    TaskRepository,
)
from meta_agent.core.ports.workspace import WorkspaceError, WorkspaceManager
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
        workspaces: WorkspaceManager | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
        config: WorkerConfig | None = None,
    ) -> None:
        self._stream = stream
        self._tasks = tasks
        self._checkpoints = checkpoints
        self._audits = audits
        self._registry = registry
        self._workspaces = workspaces
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
        # Redelivery after a finalised run (ack lost between
        # ``complete()`` and ``ack``): drain the message and audit,
        # never revive the row or run the graph again.
        if task.state in TERMINAL_TASK_STATES:
            await self._audit(
                ctx,
                "worker.task_already_terminal",
                payload={"task_id": task.task_id, "state": task.state.value},
            )
            await self._stream.ack(msg.entry_id)
            return
        graph = self._registry.resolve(task.task_type, task.graph_id)
        # Workspace lifecycle bookends the graph run: provision before
        # the first step so node code can see ``_workspace_path`` in
        # ``state.data``, cleanup in ``finally`` so a step failure or a
        # terminal-write race still releases the worktree.
        if self._registry.requires_workspace(graph.graph_id):
            if self._workspaces is None:
                await self._audit(
                    ctx,
                    "worker.workspace_unavailable",
                    payload={"task_id": task.task_id, "graph_id": graph.graph_id},
                )
                await self._stream.ack(msg.entry_id)
                return
            try:
                workspace = await self._provision_workspace(task, ctx, graph.graph_id)
            except WorkspaceError as exc:
                await self._audit(
                    ctx,
                    "workspace.provision_failed",
                    payload={"task_id": task.task_id, "error": str(exc)},
                )
                # Surface to ``_handle`` so the message stays in the
                # PEL and delivery_count increments on retry.
                raise
            try:
                await self._run_graph(msg, ctx, task, graph, workspace=workspace)
            finally:
                await self._cleanup_workspace(workspace, ctx)
        else:
            await self._run_graph(msg, ctx, task, graph, workspace=None)

    async def _run_graph(
        self,
        msg: DeliveredMessage,
        ctx: RequestContext,
        task: Task,
        graph: Graph,
        *,
        workspace: Workspace | None,
    ) -> None:
        state = await self._load_state(task, graph)
        if workspace is not None:
            # Re-seed each run; checkpoint snapshots from prior runs carry
            # a stale path (workspaces are ephemeral, never reused).
            state = state.model_copy(
                update={
                    "data": {
                        **state.data,
                        "_workspace_path": workspace.worktree_path,
                        "_workspace_branch": workspace.branch,
                    }
                }
            )
        # ``started_at`` for the result anchors on the first time this
        # task entered ``RUNNING``. For a redelivered message we trust
        # the task row's existing ``updated_at``; for the first run we
        # capture the clock right at the transition.
        started_at = task.updated_at
        if task.state != TaskState.RUNNING:
            started_at = self._clock()
            await self._tasks.update_state(
                task.tenant_id, task.task_id, TaskState.RUNNING, started_at
            )
        while not state.finished:
            state = await graph.step(state)
            await self._persist_step(task, state)
        finished_at = self._clock()
        result, terminal_state = self._build_result(task, graph, state, started_at, finished_at)
        try:
            await self._tasks.complete(
                task.tenant_id,
                task.task_id,
                result=result,
                terminal_state=terminal_state,
                updated_at=finished_at,
            )
        except IllegalTaskTransitionError:
            await self._audit(
                ctx,
                "worker.task_already_terminal",
                payload={"task_id": task.task_id, "attempted": terminal_state.value},
            )
            await self._stream.ack(msg.entry_id)
            return
        await self._audit(
            ctx,
            f"task.{terminal_state.value}",
            payload={"sequence": state.sequence, "output": result.output},
        )
        await self._stream.ack(msg.entry_id)

    async def _provision_workspace(
        self, task: Task, ctx: RequestContext, graph_id: str
    ) -> Workspace:
        assert self._workspaces is not None  # caller guards
        repo_url = task.input_payload.get("repo_url")
        base_ref = task.input_payload.get("base_ref")
        workspace = await self._workspaces.provision(
            tenant_id=task.tenant_id,
            task_id=task.task_id,
            trace_id=task.trace_id,
            branch=f"agent/{task.task_id}",
            repo_url=repo_url if isinstance(repo_url, str) else None,
            base_ref=base_ref if isinstance(base_ref, str) else None,
        )
        await self._audit(
            ctx,
            "workspace.provisioned",
            payload={
                "task_id": task.task_id,
                "graph_id": graph_id,
                "workspace_id": workspace.workspace_id,
                "branch": workspace.branch,
                "worktree_path": workspace.worktree_path,
            },
        )
        return workspace

    async def _cleanup_workspace(self, workspace: Workspace, ctx: RequestContext) -> None:
        assert self._workspaces is not None  # caller guards
        try:
            await self._workspaces.cleanup(workspace)
        except WorkspaceError as exc:
            # Best-effort: a failed cleanup must not roll back the
            # terminal state or block ack; surface it via audit and
            # leave the janitor / operator to reconcile on disk.
            await self._audit(
                ctx,
                "workspace.cleanup_failed",
                payload={
                    "workspace_id": workspace.workspace_id,
                    "worktree_path": workspace.worktree_path,
                    "error": str(exc),
                },
            )
            return
        await self._audit(
            ctx,
            "workspace.cleaned",
            payload={"workspace_id": workspace.workspace_id},
        )

    @staticmethod
    def _build_result(
        task: Task,
        graph: Graph,
        state: TaskRunState,
        started_at: datetime,
        finished_at: datetime,
    ) -> tuple[TaskResult, TaskState]:
        raw_output = state.data.get("output")
        output = raw_output if isinstance(raw_output, dict) else None
        status: TaskResultStatus
        err: TaskError | None
        terminal: TaskState
        if state.error is not None:
            terminal = TaskState.FAILED
            err = TaskError(code=TaskErrorCode.GRAPH_ERROR, message=state.error)
            status = "failed"
        else:
            terminal = TaskState.SUCCEEDED
            err = None
            status = "succeeded"
        return (
            TaskResult(
                task_id=task.task_id,
                tenant_id=task.tenant_id,
                trace_id=task.trace_id,
                graph_id=graph.graph_id,
                status=status,
                output=output,
                error=err,
                node_sequence=state.sequence,
                started_at=started_at,
                finished_at=finished_at,
            ),
            terminal,
        )

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
        if envelope.task_id is None:
            await self._stream.ack(msg.entry_id)
            return
        task = await self._tasks.get(envelope.tenant_id, envelope.task_id)
        if task is None:
            await self._stream.ack(msg.entry_id)
            return
        # If the task already reached a terminal state (e.g. a parallel
        # worker finalised it before this redelivery), don't disturb
        # the result row.
        if task.state in {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}:
            await self._stream.ack(msg.entry_id)
            return
        graph = self._registry.resolve(task.task_type, task.graph_id)
        latest = await self._checkpoints.latest(task.tenant_id, task.task_id)
        finished_at = self._clock()
        result = TaskResult(
            task_id=task.task_id,
            tenant_id=task.tenant_id,
            trace_id=task.trace_id,
            graph_id=graph.graph_id,
            status="failed",
            output=None,
            error=TaskError(
                code=TaskErrorCode.ABANDONED,
                message=f"delivery_count exceeded: {reason}",
                details={"delivery_count": msg.delivery_count},
            ),
            node_sequence=latest.sequence if latest is not None else 0,
            started_at=task.updated_at,
            finished_at=finished_at,
        )
        # Lost the race to another finaliser? The existing result is
        # authoritative — nothing more to do here.
        with contextlib.suppress(IllegalTaskTransitionError):
            await self._tasks.complete(
                task.tenant_id,
                task.task_id,
                result=result,
                terminal_state=TaskState.FAILED,
                updated_at=finished_at,
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
