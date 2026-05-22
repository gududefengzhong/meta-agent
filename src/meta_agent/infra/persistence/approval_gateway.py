"""Transactional approve / abort gateway for AWAITING_APPROVAL tasks (Phase γ-A).

The approve path is **not** a single ``UPDATE``. It has to:

1. Append a new ``task_checkpoint`` row whose ``state_snapshot``
   carries the operator's decision (and optional feedback) merged
   into ``data``, with ``awaiting_approval=False`` so the worker
   resumes past the gate;
2. Atomically transition the ``tasks`` row from ``AWAITING_APPROVAL``
   back to ``RUNNING``;
3. Enqueue a new ``OutboxEvent`` so the dispatcher re-publishes the
   task command to the queue and the next available worker picks it
   up.

Doing those three in one Postgres transaction is what makes the
operator-side API call idempotent under retries: either every row
lands or none does. The dual-write between Postgres and Redis is
absorbed by the outbox dispatcher, exactly the way the initial-submit
path does it.

The abort path is simpler — flip the row to ``CANCELLED`` and persist
a final :class:`TaskResult` — but lives here for symmetry.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import asyncpg

from meta_agent.core.domain.checkpoint import TaskCheckpoint
from meta_agent.core.domain.errors import AgentError, ErrorCategory
from meta_agent.core.domain.outbox import OutboxEvent
from meta_agent.core.domain.task import Task, TaskState
from meta_agent.core.orchestration.human_gate import (
    HUMAN_DECISION_KEY,
    HUMAN_FEEDBACK_KEY,
)
from meta_agent.core.ports.repository import IllegalTaskTransitionError
from meta_agent.infra.persistence.checkpoint_repo import PgCheckpointRepository
from meta_agent.infra.persistence.outbox_repo import PgOutboxRepository
from meta_agent.infra.persistence.pool import DatabasePool
from meta_agent.infra.persistence.task_repo import PgTaskRepository


class TaskNotAwaitingApprovalError(AgentError):
    """Raised when approve / abort is called on a task that is not paused.

    Caused by missing task, wrong tenant, or a state that is not
    ``AWAITING_APPROVAL`` (e.g. the task already completed, or two
    operators raced the approve and one already won). LOGIC category
    because retrying the same call blindly will not help.
    """

    category = ErrorCategory.LOGIC


class TaskApprovalGateway:
    """Composed approve / abort orchestration over the Phase γ-A repos."""

    def __init__(
        self,
        *,
        pool: DatabasePool,
        task_repo: PgTaskRepository,
        checkpoint_repo: PgCheckpointRepository,
        outbox_repo: PgOutboxRepository,
        task_topic: str,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._pool = pool
        self._tasks = task_repo
        self._checkpoints = checkpoint_repo
        self._outbox = outbox_repo
        self._topic = task_topic
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))

    async def approve(
        self,
        tenant_id: str,
        task_id: str,
        *,
        feedback: str | None = None,
    ) -> Task:
        """Resume a paused task with an optional operator feedback string.

        Returns the refreshed :class:`Task` (now in ``RUNNING``). Raises
        :class:`TaskNotAwaitingApprovalError` if the task is missing,
        not paused, or already raced through to another state.
        """

        return await self._resolve(
            tenant_id,
            task_id,
            decision="approve",
            feedback=feedback,
            next_state=TaskState.RUNNING,
        )

    async def reject(
        self,
        tenant_id: str,
        task_id: str,
        *,
        feedback: str | None = None,
    ) -> Task:
        """Reject the pending step (graph routes to END as ``_rejected_by_human=True``).

        Unlike :meth:`abort`, ``reject`` does not terminate the task —
        the graph continues running but the rejected step takes the
        ``next_node_when_approved`` branch's *reject* fall-through (the
        :class:`human_gate` node routes to END with a rejection marker
        on the state). Use this when the operator wants the agent to
        clean up gracefully rather than slam the brakes.
        """

        return await self._resolve(
            tenant_id,
            task_id,
            decision="reject",
            feedback=feedback,
            next_state=TaskState.RUNNING,
        )

    async def abort(
        self,
        tenant_id: str,
        task_id: str,
        *,
        reason: str | None = None,
    ) -> Task:
        """Terminate a paused task as ``CANCELLED``.

        No checkpoint is appended (the task does not resume) and no
        :class:`TaskResult` is written — ``cancelled`` lives outside
        the result contract by design, so ``get_result()`` returns
        ``None`` for an operator-aborted task. ``reason`` is reserved
        for audit emission in γ-B and is dropped here.
        """

        # ``reason`` deliberately unused at γ-A; γ-B emits it via audit.
        del reason
        now = self._clock()
        try:
            await self._tasks.transition_from_awaiting_approval(
                tenant_id,
                task_id,
                TaskState.CANCELLED,
                now,
            )
        except IllegalTaskTransitionError as exc:
            raise TaskNotAwaitingApprovalError(str(exc)) from exc
        refreshed = await self._tasks.get(tenant_id, task_id)
        assert refreshed is not None  # the transition_* write succeeded
        return refreshed

    async def _resolve(
        self,
        tenant_id: str,
        task_id: str,
        *,
        decision: str,
        feedback: str | None,
        next_state: TaskState,
    ) -> Task:
        now = self._clock()
        current = await self._tasks.get(tenant_id, task_id)
        if current is None or current.state != TaskState.AWAITING_APPROVAL:
            raise TaskNotAwaitingApprovalError(f"task {task_id!r} is not in AWAITING_APPROVAL")
        latest_checkpoint = await self._checkpoints.latest(tenant_id, task_id)
        if latest_checkpoint is None:
            raise TaskNotAwaitingApprovalError(f"task {task_id!r} has no checkpoint to resume from")
        new_snapshot = _merge_decision(
            snapshot=latest_checkpoint.state_snapshot,
            decision=decision,
            feedback=feedback,
            sequence=latest_checkpoint.sequence + 1,
        )
        new_checkpoint = TaskCheckpoint(
            checkpoint_id=self._id_factory(),
            task_id=task_id,
            tenant_id=tenant_id,
            trace_id=latest_checkpoint.trace_id,
            node_name=latest_checkpoint.node_name,
            sequence=latest_checkpoint.sequence + 1,
            state_snapshot=new_snapshot,
            created_at=now,
        )
        event = OutboxEvent(
            event_id=self._id_factory(),
            tenant_id=tenant_id,
            trace_id=current.trace_id,
            aggregate_type="task",
            aggregate_id=task_id,
            topic=self._topic,
            payload=dict(current.input_payload),
            idempotency_key=f"resume:{task_id}:{new_checkpoint.sequence}",
            created_at=now,
        )
        try:
            async with self._pool.transaction() as conn:
                await self._checkpoints.append_in_conn(new_checkpoint, conn)
                await self._tasks.transition_from_awaiting_approval(
                    tenant_id,
                    task_id,
                    next_state,
                    now,
                    conn=conn,
                )
                await self._outbox.enqueue_in_conn(event, conn)
        except IllegalTaskTransitionError as exc:
            raise TaskNotAwaitingApprovalError(str(exc)) from exc
        except asyncpg.UniqueViolationError as exc:
            # Either the new checkpoint id collided (impossible in
            # practice; uuid4) or the outbox idempotency_key matched a
            # prior approve call — both surface as a no-op for the
            # caller, with the existing row authoritative.
            raise TaskNotAwaitingApprovalError(
                f"approve / reject for task {task_id!r} already applied"
            ) from exc
        refreshed = await self._tasks.get(tenant_id, task_id)
        assert refreshed is not None
        return refreshed


def _merge_decision(
    *,
    snapshot: dict[str, Any],
    decision: str,
    feedback: str | None,
    sequence: int,
) -> dict[str, Any]:
    """Return the snapshot a worker should resume from after approval."""

    new = dict(snapshot)
    new["awaiting_approval"] = False
    new["sequence"] = sequence
    data = dict(new.get("data", {}))
    data[HUMAN_DECISION_KEY] = decision
    if feedback is not None:
        data[HUMAN_FEEDBACK_KEY] = feedback
    new["data"] = data
    return new
