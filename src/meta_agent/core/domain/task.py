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
    ``AUTO_PR`` / ``FEATURE_IMPL``) are defined in
    ``docs/specs/AGENT_SPEC.md`` (§L1 + Phase β+).
    System families (prefixed ``system_``) are reserved for built-in
    self-tests / smoke flows and never carry customer-facing semantics.
    The enum is open for extension but closed for renaming.
    """

    BUG_FIX = "bug_fix"
    CODE_REVIEW = "code_review"
    AUTO_PR = "auto_pr"
    FEATURE_IMPL = "feature_impl"
    SYSTEM_ECHO = "system_echo"
    SYSTEM_CHAT = "system_chat"
    SYSTEM_GIT_INSPECT = "system_git_inspect"
    SYSTEM_SHELL_AGENT = "system_shell_agent"


class TaskState(StrEnum):
    """Lifecycle states of a task.

    Transitions are enforced by the orchestration layer; this model
    only declares the value set.

    ``AWAITING_APPROVAL`` is the Phase γ pause state used when a graph
    hits a ``human_gate`` node (PermissionMode gate or BudgetPolicy
    gate). It is *not* terminal — an API call to
    ``POST /v1/tasks/{id}/approve`` transitions it back to
    ``RUNNING`` and the worker resumes from the latest checkpoint.

    ``EXPIRED`` is the terminal landing zone for tasks that have sat
    in ``AWAITING_APPROVAL`` past the long-tail sweeper threshold; it
    is distinct from ``CANCELLED`` (explicit user abort) and
    ``FAILED`` (an in-flight failure) so analytics can tell apart
    abandoned tasks from rejected ones.
    """

    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class PermissionMode(StrEnum):
    """Per-task human-in-the-loop policy (Phase γ + δ-1).

    Composes orthogonally with :class:`BudgetPolicy`. ``auto`` is the
    legacy zero-friction behaviour; the ``approve_*`` modes inject
    explicit ``human_gate`` checkpoints at well-defined points in the
    graph topology.

    Inline-permission modes (δ-1) use the :class:`PermissionGate`
    rendezvous instead of the operator-driven AWAITING_APPROVAL flow:

    * ``approve_each_tool`` — gate before every tool call (granular,
      can feel noisy on long sessions)
    * ``plan`` — gate once per *planning step* (the assistant's
      message + its proposed batch of tool calls). Right balance for
      "tell me what you're going to do, I'll approve, then go" UX.
      After approval the planning step's tool calls all execute; the
      next planning step triggers a fresh prompt
    """

    AUTO = "auto"
    APPROVE_BEFORE_PUSH = "approve_before_push"
    APPROVE_EACH_TOOL = "approve_each_tool"
    PLAN = "plan"


class BudgetPolicy(StrEnum):
    """Per-task budget-threshold reaction (Phase γ).

    Composes orthogonally with :class:`PermissionMode`. The threshold
    itself is read from the tenant's monthly LLM budget for now;
    γ-C generalises it to per-task hard ceilings.
    """

    NONE = "none"
    GATE_ON_THRESHOLD = "gate_on_threshold"
    ABORT_ON_THRESHOLD = "abort_on_threshold"


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
    permission_mode: PermissionMode = PermissionMode.AUTO
    budget_policy: BudgetPolicy = BudgetPolicy.NONE
    # Phase γ-C per-task cost ceiling in micro-USD. The
    # :class:`BudgetPolicy` only fires when this is non-null; ``None``
    # means "no per-task ceiling, only tenant-level monthly limits
    # apply".
    budget_threshold_micros: int | None = Field(default=None, ge=0)
    input_payload: dict[str, object] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
