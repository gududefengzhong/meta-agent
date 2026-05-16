"""API request / response schemas.

These are the public shapes of the task submission API.  They are
deliberately separate from the domain models so the wire format can
evolve independently of internal storage layout.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from meta_agent.core.domain.task import TaskState, TaskType
from meta_agent.core.orchestration.result import TaskError, TaskResultStatus


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
