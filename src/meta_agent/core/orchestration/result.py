"""Result contract for a finished task run.

A :class:`TaskResult` is the public, JSON-stable summary of how a
task ended. It is the single payload the worker writes alongside the
terminal state via :meth:`TaskRepository.complete`, so downstream
consumers (API, MCP resources, billing, audit) never have to introspect
``TaskRunState.data`` or guess at the shape of a graph's scratch space.

Design rules
------------
* Frozen pydantic v2 model with ``extra="forbid"`` so a typo in any
  graph or worker writer surfaces at construction time, not at read.
* ``status`` is restricted to the two outcomes that can land in the
  result table: ``"succeeded"`` and ``"failed"``. ``cancelled`` is a
  separate lifecycle action handled outside the result contract.
* ``error`` is required iff ``status == "failed"`` and forbidden
  otherwise; a model validator enforces this invariant.
* ``output`` is whatever JSON-safe dict the graph chose to expose
  under ``state.data["output"]``; we do not project ``state.data``
  wholesale (it can contain scratch / internal keys).
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

TaskResultStatus = Literal["succeeded", "failed"]


class TaskErrorCode(StrEnum):
    """Coarse classification of why a task ended in ``failed``.

    The enum is intentionally narrow at P1-F.1; richer classification
    (per-LLM-error-class, per-tool-class, ...) will be layered on top
    once we have real producers of those signals. New codes are
    *additive*: never rename an existing value.
    """

    # A graph node finished normally but set ``state.error``.
    GRAPH_ERROR = "graph_error"
    # The message was redelivered past ``WorkerConfig.max_attempts``.
    ABANDONED = "abandoned"
    # Catch-all for worker-side faults that don't map to a more
    # specific code yet (e.g. an unexpected exception in graph plumbing).
    INTERNAL = "internal"


class TaskError(BaseModel):
    """Structured failure descriptor attached to a failed :class:`TaskResult`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: TaskErrorCode
    message: str = Field(..., min_length=1)
    # Optional adapter-specific bag for debugging / observability. Must
    # be JSON-safe; we don't validate the contents here because the
    # column is JSONB and callers own the shape.
    details: dict[str, Any] | None = None


class TaskResult(BaseModel):
    """Public, JSON-stable summary of a finished task run.

    Persisted as a single ``tasks.result_json`` row, paired atomically
    with the terminal :class:`TaskState`. See
    :meth:`meta_agent.core.ports.repository.TaskRepository.complete`
    for the write contract.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    graph_id: str = Field(..., min_length=1)
    status: TaskResultStatus
    # Whatever public dict the graph wrote into ``state.data["output"]``.
    # ``None`` means the graph produced no externally visible output
    # (acceptable on failure paths and for diagnostic-only flows).
    output: dict[str, Any] | None = None
    error: TaskError | None = None
    # Final ``TaskRunState.sequence`` reached. Useful for joining the
    # result back to the checkpoint stream without an extra query.
    node_sequence: int = Field(..., ge=0)
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def _check_status_error_invariant(self) -> Self:
        if self.status == "succeeded" and self.error is not None:
            raise ValueError("TaskResult.error must be None when status='succeeded'")
        if self.status == "failed" and self.error is None:
            raise ValueError("TaskResult.error is required when status='failed'")
        if self.finished_at < self.started_at:
            raise ValueError("TaskResult.finished_at must be >= started_at")
        return self
