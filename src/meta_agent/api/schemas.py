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
from meta_agent.core.domain.task import TaskState, TaskType
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


class TaskResponse(BaseModel):
    """Shape returned by ``POST /v1/tasks`` and ``GET /v1/tasks/{task_id}``."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    tenant_id: str
    state: TaskState
    task_type: TaskType
    trace_id: str
    session_id: str | None
    created_at: datetime
    updated_at: datetime


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
