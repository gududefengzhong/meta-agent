"""API request / response schemas.

These are the public shapes of the task submission API.  They are
deliberately separate from the domain models so the wire format can
evolve independently of internal storage layout.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from meta_agent.core.domain.errors import ErrorCategory
from meta_agent.core.domain.llm_usage import LLMUsageStatus
from meta_agent.core.domain.task import BudgetPolicy, PermissionMode, TaskState, TaskType
from meta_agent.core.orchestration.result import TaskError, TaskResultStatus
from meta_agent.core.ports.llm_usage import UsageGroupBy

_BUG_FIX_GRAPH_ID = "builtin.bug_fix"


class SubmitTaskRequest(BaseModel):
    """Body of ``POST /v1/tasks``."""

    model_config = ConfigDict(extra="forbid")

    task_type: TaskType
    input_payload: dict[str, object] = Field(default_factory=dict)
    # Optional explicit graph override.  None lets the worker choose the
    # registered default for the task_type.
    graph_id: str | None = Field(default=None, min_length=1)
    # Caller-supplied idempotency key.  When present the DB unique index
    # (tenant_id, idempotency_key) ensures exactly-once submission.
    idempotency_key: str | None = Field(default=None, min_length=1)
    session_id: str | None = Field(default=None, min_length=1)
    # Phase γ-A trust-surface configuration. Defaults preserve the
    # legacy zero-friction behaviour; callers opt into human-in-the-loop
    # gates by setting ``permission_mode``.
    permission_mode: PermissionMode = PermissionMode.AUTO
    budget_policy: BudgetPolicy = BudgetPolicy.NONE
    budget_threshold_micros: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_bug_fix_payload(self) -> SubmitTaskRequest:
        """Enforce a typed payload for the bug-fix product path."""

        if self.task_type is TaskType.BUG_FIX:
            payload = BugFixInputPayload.model_validate(self.input_payload)
            self.input_payload = payload.model_dump(mode="json", exclude_none=True)
            if self.graph_id is not None and self.graph_id != _BUG_FIX_GRAPH_ID:
                raise ValueError(f"bug_fix tasks may only target graph_id={_BUG_FIX_GRAPH_ID!r}")
        return self

    @model_validator(mode="after")
    def _validate_permission_and_budget_policy(self) -> SubmitTaskRequest:
        """Keep the public REST contract aligned with the product surface."""

        if self.permission_mode not in (
            PermissionMode.AUTO,
            PermissionMode.APPROVE_BEFORE_PUSH,
        ):
            raise ValueError(
                "REST task submission supports only permission_mode=auto or approve_before_push"
            )
        if self.budget_policy is BudgetPolicy.NONE:
            if self.budget_threshold_micros is not None:
                raise ValueError("budget_threshold_micros requires a non-none budget_policy")
        elif self.budget_threshold_micros is None:
            raise ValueError(
                "budget_threshold_micros is required when budget_policy is gate_on_threshold "
                "or abort_on_threshold"
            )
        return self


class BugFixInputPayload(BaseModel):
    """Typed input contract for the API-first bug-fix agent."""

    model_config = ConfigDict(extra="forbid")

    issue_description: str = Field(..., min_length=1)
    repo_url: str = Field(..., min_length=1)
    base_ref: str | None = Field(default=None, min_length=1)
    target_files: list[str] = Field(..., min_length=1)
    verify_suite: Literal[
        "python_test",
        "python_lint",
        "typescript_typecheck",
        "typescript_test",
    ] = "python_test"
    model: str | None = Field(default=None, min_length=1)
    max_steps: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_target_files(self) -> BugFixInputPayload:
        if not all(path.strip() for path in self.target_files):
            raise ValueError("target_files must contain non-empty paths only")
        return self


class TaskResponse(BaseModel):
    """Shape returned by ``POST /v1/tasks`` and ``GET /v1/tasks/{task_id}``."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    tenant_id: str
    state: TaskState
    task_type: TaskType
    trace_id: str
    session_id: str | None
    permission_mode: PermissionMode
    budget_policy: BudgetPolicy
    budget_threshold_micros: int | None
    created_at: datetime
    updated_at: datetime


class ApprovalRequest(BaseModel):
    """Body of ``POST /v1/tasks/{task_id}/approve`` and ``/reject``.

    ``feedback`` is an optional free-text hint injected into the
    resumed graph state under ``_human_feedback``; downstream nodes
    can incorporate it (e.g. as a replan hint).
    """

    model_config = ConfigDict(extra="forbid")

    feedback: str | None = Field(default=None, max_length=10_000)


class AbortRequest(BaseModel):
    """Body of ``POST /v1/tasks/{task_id}/abort``.

    ``reason`` is reserved for audit emission (γ-B); ignored at γ-A.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=1_000)


class SessionResponse(BaseModel):
    """Shape returned by ``GET /v1/sessions/{session_id}``."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    tenant_id: str
    principal_id: str
    created_at: datetime
    last_active_at: datetime
    is_closed: bool


class SessionMessage(BaseModel):
    """One reconstructed conversation message from the session's task history.

    The thread is derived from the tasks in the session — see
    :func:`build_prior_messages` for the field-extraction contract.
    """

    model_config = ConfigDict(extra="forbid")

    role: str
    content: str
    task_id: str
    created_at: datetime


class SessionMessagesResponse(BaseModel):
    """Shape returned by ``GET /v1/sessions/{session_id}/messages``."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    messages: list[SessionMessage]


class TrajectoryResponse(BaseModel):
    """Body of ``GET /v1/tasks/{task_id}/trajectory``.

    The wire shape forwards the domain :class:`TrajectoryPage` 1:1 so
    clients can rely on the discriminated-union ``kind`` field
    (``"audit"`` / ``"checkpoint"`` / ``"usage"``) to render each row.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[dict[str, Any]]
    truncated: bool = False


class TaskResultResponse(BaseModel):
    """Shape returned by ``GET /v1/tasks/{task_id}/result``."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: TaskResultStatus
    graph_id: str
    output: dict[str, Any] | None
    error: TaskError | None
    node_sequence: int
    started_at: datetime
    finished_at: datetime


class TaskObservabilitySummaryResponse(BaseModel):
    """Shape returned by ``GET /v1/tasks/{task_id}/observability``."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    state: TaskState
    result_status: TaskResultStatus | None
    verifier_passed: bool | None
    failure_category: str | None
    failure_kind: str | None
    attempts: int | None
    files_changed: list[str]
    patch_present: bool
    llm_calls: int = Field(..., ge=0)
    llm_failures: int = Field(..., ge=0)
    total_tokens: int = Field(..., ge=0)
    total_cost_usd_micros: int = Field(..., ge=0)
    total_latency_ms: int = Field(..., ge=0)
    tool_events: int = Field(..., ge=0)
    tool_failures: int = Field(..., ge=0)
    human_interventions: int = Field(..., ge=0)
    budget_outcome: str
    auto_pr_child_status: str
    cost_by_step_kind: dict[str, int]
    models: list[str]


# ── Query API responses ──────────────────────────────────────────────────────


class AuditEventResponse(BaseModel):
    """Wire shape of one ``audit_events`` row."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    tenant_id: str
    principal_id: str
    session_id: str | None
    task_id: str | None
    trace_id: str
    action: str
    payload: dict[str, Any]
    occurred_at: datetime


class AuditListResponse(BaseModel):
    """Body of ``GET /v1/audits``: a page of events + keyset cursor.

    ``next_cursor`` is ``None`` on the last page; clients pass it back
    via ``?cursor=`` to fetch the next page in DESC order.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[AuditEventResponse]
    next_cursor: str | None = None


class LLMUsageResponse(BaseModel):
    """Wire shape of one ``llm_usage_logs`` row."""

    model_config = ConfigDict(extra="forbid")

    record_id: str
    tenant_id: str
    trace_id: str
    request_id: str | None
    principal_id: str | None
    session_id: str | None
    task_id: str | None
    provider: str
    model: str | None
    requested_model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    finish_reason: str | None
    provider_response_id: str | None
    cost_usd_micros: int | None
    latency_ms: int
    status: LLMUsageStatus
    error_category: ErrorCategory | None
    error_message: str | None
    prompt_excerpt: str | None = None
    created_at: datetime


class LLMUsageListResponse(BaseModel):
    """Body of ``GET /v1/usages`` (list mode): page of records + cursor."""

    model_config = ConfigDict(extra="forbid")

    items: list[LLMUsageResponse]
    next_cursor: str | None = None


class UsageAggregateResponse(BaseModel):
    """One bucket of a grouped usage aggregate."""

    model_config = ConfigDict(extra="forbid")

    key: str
    tokens: int = Field(..., ge=0)
    cost_usd_micros: int = Field(..., ge=0)
    calls: int = Field(..., ge=0)


class UsageAggregateListResponse(BaseModel):
    """Body of ``GET /v1/usages?group_by=...``: full bucket set, no cursor.

    Aggregations are always returned in one shot (callers cap the
    window themselves); pagination is intentionally not supported here
    because the bucket count is bounded by the cardinality of the
    grouping key, not by the row count.
    """

    model_config = ConfigDict(extra="forbid")

    items: list[UsageAggregateResponse]
    group_by: UsageGroupBy
    since: datetime
    until: datetime
