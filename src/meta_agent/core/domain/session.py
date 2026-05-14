"""Session model.

A session is a long-lived user-facing context that can span multiple
tasks. See ``docs/specs/CONTEXT_PROPAGATION.md`` for the ID contract.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Session(BaseModel):
    """A user-facing session.

    A session belongs to exactly one tenant and one principal. Sessions
    are never reused across tenants.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    principal_id: str = Field(..., min_length=1)
    created_at: datetime
    last_active_at: datetime
    is_closed: bool = False
