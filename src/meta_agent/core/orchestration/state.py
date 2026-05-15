"""Task run state for the LangGraph-style orchestration runtime.

The state is the only object that flows between graph nodes. It is
intentionally immutable (``frozen=True``) so that every checkpoint
corresponds to a single, hash-stable snapshot of execution; mutation
happens by returning a new instance via :meth:`TaskRunState.advance`.

Serialization is delegated to pydantic v2: ``model_dump(mode="json")``
produces the JSON-safe dict that the checkpoint repository persists,
and ``model_validate`` rehydrates a state from such a dict.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field

START: str = "__start__"
END: str = "__end__"


class TaskRunState(BaseModel):
    """Immutable snapshot of a single task's orchestration progress.

    The triplet ``(tenant_id, trace_id, task_id)`` is fixed for the
    lifetime of a run and must never be mutated. ``current_node`` is
    the next node to execute; ``START`` before the first step and
    ``END`` once the graph has completed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    graph_id: str = Field(..., min_length=1)
    current_node: str = Field(default=START, min_length=1)
    sequence: int = Field(default=0, ge=0)
    data: dict[str, object] = Field(default_factory=dict)
    finished: bool = False
    error: str | None = None

    def advance(
        self,
        *,
        next_node: str,
        data_update: dict[str, object] | None = None,
        finished: bool | None = None,
        error: str | None = None,
    ) -> Self:
        """Return a new state with the cursor moved and ``data`` merged.

        ``data_update`` is shallow-merged into ``data``; callers wanting
        to drop a key must pass an explicit sentinel value rather than
        relying on absence. ``finished`` defaults to ``True`` exactly
        when ``next_node`` equals :data:`END`.
        """

        merged: dict[str, object] = dict(self.data)
        if data_update:
            merged.update(data_update)
        resolved_finished = finished if finished is not None else next_node == END
        return self.model_copy(
            update={
                "current_node": next_node,
                "sequence": self.sequence + 1,
                "data": merged,
                "finished": resolved_finished,
                "error": error,
            }
        )
