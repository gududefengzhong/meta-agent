"""Task model.

A task is the unit of asynchronous, durable, recoverable work executed
by an agent worker. See ``docs/specs/AGENT_SPEC.md`` §L1 for the three
first-class task families (Bug Fix / Code Review / Auto PR).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class TaskType(StrEnum):
    """Task families.

    First-class business families (``BUG_FIX`` / ``CODE_REVIEW`` /
    ``AUTO_PR``) are defined in ``docs/specs/AGENT_SPEC.md`` §L1.
    System families (prefixed ``system_``) are reserved for built-in
    self-tests / smoke flows and never carry customer-facing semantics.
    The enum is open for extension but closed for renaming.
    """

    BUG_FIX = "bug_fix"
    CODE_REVIEW = "code_review"
    AUTO_PR = "auto_pr"
    SYSTEM_ECHO = "system_echo"
    SYSTEM_CHAT = "system_chat"


class TaskState(StrEnum):
    """Lifecycle states of a task.

    Transitions are enforced by the orchestration layer; this model
    only declares the value set.
    """

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_HUMAN = "awaiting_human"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(BaseModel):
    """A unit of asynchronous, recoverable agent work.

    Every task carries the full context contract (``tenant_id``,
    ``session_id``, ``trace_id``) so that audit, billing and trace
    records can be joined across stores.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    session_id: str | None = None
    principal_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    idempotency_key: str | None = None
    task_type: TaskType
    # Explicit graph override. ``None`` lets the worker resolve the
    # default graph for ``task_type`` via the orchestration registry;
    # a non-empty string pins this run to a specific graph_id.
    graph_id: str | None = Field(default=None, min_length=1)
    state: TaskState = TaskState.PENDING
    input_payload: dict[str, object] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
