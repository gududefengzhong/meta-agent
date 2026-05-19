"""Port for enqueueing follow-up tasks atomically with audit boundaries.

Used by :class:`meta_agent.worker.runner.WorkerLoop` after a graph
finishes in :data:`TaskState.SUCCEEDED` to materialise the next task in
a multi-step pipeline (e.g. ``BUG_FIX`` ‚Üí ``AUTO_PR``). The adapter is
expected to write the new ``tasks`` row and its accompanying
``outbox_events`` row inside a single PG transaction so that recovery
after a crash either replays the chain step or skips it cleanly via
the configured ``idempotency_key``.

Policy lives in :mod:`meta_agent.core.orchestration.chain`; this port
only describes the persistence contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel, ConfigDict, Field

from meta_agent.core.domain.task import Task, TaskType


class FollowUpSpec(BaseModel):
    """A pending follow-up task derived from a completed parent.

    The spec is the policy-layer's narrow contract with the persistence
    layer: the policy decides ``what`` and ``why`` (which task type,
    which payload), the adapter decides ``how`` (which connection,
    which transaction). ``idempotency_key`` is required and is the
    sole mechanism the chain relies on to deduplicate follow-ups when
    the parent's completion message is redelivered.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_type: TaskType
    input_payload: dict[str, object] = Field(default_factory=dict)
    idempotency_key: str = Field(..., min_length=1)
    topic: str = Field(..., min_length=1)


class TaskSubmitter(ABC):
    """Persist a follow-up task atomically with its outbox event."""

    @abstractmethod
    async def submit_follow_up(
        self,
        parent: Task,
        follow_up: FollowUpSpec,
    ) -> Task | None:
        """Enqueue ``follow_up`` as a new task chained from ``parent``.

        Returns the newly persisted :class:`Task` on success, or
        ``None`` if the chain step has already been recorded (a
        redelivered parent completion observed the same
        ``(tenant_id, idempotency_key)`` unique constraint). Adapters
        must not raise on duplicate keys ‚Äî that signal belongs in the
        return type so callers can audit ``chain_skipped`` instead of
        ``chain_failed``.
        """
