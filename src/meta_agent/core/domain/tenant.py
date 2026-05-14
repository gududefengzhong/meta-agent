"""Tenant model.

【当前】最小字段；后续 RBAC、SSO、配额等扩展见 AGENT_SPEC.md §多租户与权限。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Tenant(BaseModel):
    """A tenant represents an isolation boundary across requests, tasks,
    sessions, audit and billing records.

    The ``tenant_id`` value is propagated end-to-end and is the root of
    the multi-tenant isolation contract defined in
    ``docs/specs/CONTEXT_PROPAGATION.md``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(..., min_length=1, description="Stable tenant identifier")
    display_name: str = Field(..., min_length=1)
    created_at: datetime
    is_active: bool = True
