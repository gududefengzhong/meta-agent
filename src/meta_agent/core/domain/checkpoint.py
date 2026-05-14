"""Task checkpoint model.

Checkpoints externalize orchestration state so any worker replica can
resume a task after a failure. See ``docs/specs/AGENT_SPEC.md`` §高可用
and the Phase 0 acceptance criteria.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class TaskCheckpoint(BaseModel):
    """A persisted snapshot of orchestration state for a task.

    The ``state_snapshot`` carries the LangGraph-style state object as
    a plain dict; serialization shape is owned by the orchestration
    layer and is intentionally opaque here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    node_name: str = Field(..., min_length=1, description="Last completed graph node")
    sequence: int = Field(..., ge=0, description="Monotonic checkpoint sequence")
    state_snapshot: dict[str, object]
    created_at: datetime
