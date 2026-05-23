"""API request / response schemas.

These are the public shapes of the task submission API.  They are
deliberately separate from the domain models so the wire format can
evolve independently of internal storage layout.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from meta_agent.core.domain.errors import ErrorCategory
from meta_agent.core.domain.llm_usage import LLMUsageStatus
from meta_agent.core.domain.task import BudgetPolicy, PermissionMode, TaskState, TaskType
from meta_agent.core.orchestration.result import TaskError, TaskResultStatus
from meta_agent.core.ports.llm_usage import UsageGroupBy


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


class PermissionDecisionRequest(BaseModel):
    """Body of ``POST /v1/tasks/{task_id}/permissions/{prompt_id}/decide``.

    The client renders a :class:`PermissionPrompt` from the
    lifecycle / permission stream, asks the user, then POSTs this
    decision. ``reason`` flows into the agent loop on deny so the
    model can plan an alternative.
    """

    model_config = ConfigDict(extra="forbid")

    allow: bool
    reason: str | None = Field(default=None, max_length=1_000)


class PermissionDecisionResponse(BaseModel):
    """Confirms the decision was accepted and routed to the waiting worker."""

    model_config = ConfigDict(extra="forbid")

    prompt_id: str
    allow: bool


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
