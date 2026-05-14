"""Billing event model.

Every LLM invocation must produce a billing event per
``docs/specs/AGENT_SPEC.md`` §计费与成本治理 and the L0 cost-visibility
constraint.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class BillingEvent(BaseModel):
    """An append-only billing record for a single billable action.

    Currency is captured explicitly to support multi-region pricing.
    Token counts are split into prompt/completion to support multiple
    upstream models; both can be zero for non-LLM billable actions.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    principal_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None
    trace_id: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1, description="Routed model identifier")
    provider: str = Field(..., min_length=1, description="Upstream provider, e.g. openrouter")
    prompt_tokens: int = Field(..., ge=0)
    completion_tokens: int = Field(..., ge=0)
    total_tokens: int = Field(..., ge=0)
    cost: Decimal = Field(..., ge=Decimal("0"))
    currency: str = Field(..., min_length=3, max_length=3)
    occurred_at: datetime
