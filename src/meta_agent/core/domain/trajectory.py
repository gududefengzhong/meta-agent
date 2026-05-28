"""Task trajectory items (Phase γ-B).

The trajectory is a time-ordered merge of three append-only streams
that already exist in the system:

* ``audit_events`` — every state transition, audit hook, gate event
* ``task_checkpoints`` — per-step graph state snapshots
* ``llm_usage_logs`` — every LLM invocation with token / cost / prompt-id

A trajectory query returns a single list of these items ordered by
their occurrence timestamp so an operator (or a future Web UI) can
replay what the agent did, why, and what it cost — without writing
three separate queries or correlating ids by hand.

The three item shapes are deliberately distinct pydantic models with
a literal ``kind`` discriminator rather than a polymorphic base
class; the API layer serialises them through the discriminator and
clients pattern-match on it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class TrajectoryAuditItem(BaseModel):
    """One row from ``audit_events`` projected for trajectory display."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["audit"] = "audit"
    occurred_at: datetime
    event_id: str
    action: str
    payload: dict[str, Any]


class TrajectoryCheckpointItem(BaseModel):
    """One row from ``task_checkpoints`` projected for trajectory display.

    The full ``state_snapshot`` is NOT inlined here — it can be large
    (entire conversation transcript, file diffs, tool observations) and
    operators rarely need it in a list view. We surface just the
    structural fields and let the future drill-down API
    (``GET /v1/tasks/{id}/checkpoints/{sequence}``) return the snapshot.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["checkpoint"] = "checkpoint"
    occurred_at: datetime
    checkpoint_id: str
    sequence: int
    node_name: str
    current_node: str | None = None
    awaiting_approval: bool = False
    finished: bool = False


class TrajectoryUsageItem(BaseModel):
    """One row from ``llm_usage_logs`` projected for trajectory display.

    All fields are nullable in the source schema (provider failures
    surface before tokens / model are known); we forward those nulls
    rather than substituting defaults so analytics can tell apart
    "zero cost" from "unknown cost".
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["usage"] = "usage"
    occurred_at: datetime
    record_id: str
    provider: str
    model: str | None = None
    requested_model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd_micros: int | None = None
    latency_ms: int = Field(..., ge=0)
    status: str
    error_category: str | None = None
    error_message: str | None = None
    prompt_id: str | None = None
    prompt_version: int | None = None
    prompt_excerpt: str | None = None
    step_kind: str | None = None


TrajectoryItem = TrajectoryAuditItem | TrajectoryCheckpointItem | TrajectoryUsageItem
"""Discriminated union of the three trajectory variants.

API serialisation relies on the ``kind`` literal; consumers pattern-
match on it to decide how to render each row.
"""


class TrajectoryPage(BaseModel):
    """One page of trajectory items.

    ``truncated`` is ``True`` when any of the three underlying queries
    hit its row cap; the operator should narrow the window or use the
    paginated drill-down APIs (delivered in a later γ PR).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[TrajectoryItem, ...]
    truncated: bool = False
