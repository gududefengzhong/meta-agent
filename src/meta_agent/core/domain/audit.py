"""Audit event model.

Audit events capture every security-, governance- and human-confirmation
relevant action across the system. They are append-only and must be
queryable by any of ``tenant_id`` / ``session_id`` / ``task_id`` /
``trace_id`` per ``docs/specs/CONTEXT_PROPAGATION.md``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AuditEvent(BaseModel):
    """An append-only audit record.

    Concrete event types (e.g. ``task.submitted``, ``human.approved``,
    ``patch.applied``) are conveyed as ``action`` strings; the schema
    of ``payload`` is owned by the producer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    principal_id: str = Field(..., min_length=1)
    session_id: str | None = None
    task_id: str | None = None
    trace_id: str = Field(..., min_length=1)
    action: str = Field(..., min_length=1, description="Dotted action name")
    payload: dict[str, object] = Field(default_factory=dict)
    occurred_at: datetime
