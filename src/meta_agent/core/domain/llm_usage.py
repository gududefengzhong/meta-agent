"""LLM usage / cost log model.

Per ``docs/specs/AGENT_SPEC.md`` L0 cost-visibility constraint, every
LLM invocation must produce a record carrying ``tenant_id``,
``task_id``, model identity, token counts, finish reason and the
upstream response id. This module owns the per-call raw log.

The aggregated, currency-denominated billing record
(:class:`meta_agent.core.domain.BillingEvent`) is a separate concept
intended to be derived from these raw logs by a downstream pricing /
billing pipeline; the two models are deliberately not unified because
the billing model requires non-null cost and currency while the raw
log honestly admits that pricing may not yet be wired in.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from meta_agent.core.domain.errors import ErrorCategory


class LLMUsageStatus(StrEnum):
    """Outcome of a single LLM call."""

    OK = "ok"
    ERROR = "error"


class LLMUsageRecord(BaseModel):
    """An append-only record of one LLM invocation.

    Token counts mirror the port-level :class:`LLMUsage` contract:
    ``None`` means "the upstream did not report this value", not zero,
    so billing aggregation must treat unknown as unknown.

    ``cost_usd_micros`` is denominated in USD * 1_000_000 (micro-USD)
    to keep persistence integer-only. It is nullable because pricing
    resolution is intentionally out of scope for this milestone; rows
    written with ``cost_usd_micros=None`` mean "tokens known, cost not
    yet resolved" and can be back-filled by a pricing pipeline later.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    trace_id: str = Field(..., min_length=1)
    request_id: str | None = None
    principal_id: str | None = None
    session_id: str | None = None
    task_id: str | None = None

    provider: str = Field(..., min_length=1, description="Upstream provider id, e.g. openrouter")
    model: str | None = Field(
        default=None,
        description="Model actually served by the provider; None on pre-response failures",
    )
    requested_model: str | None = Field(
        default=None,
        description="Model id the caller asked for, when distinct from provider default",
    )

    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)

    finish_reason: str | None = None
    provider_response_id: str | None = None

    # Phase β+ prompt provenance. Nullable because not every call
    # originates from a registered prompt (smoke harnesses, ad-hoc
    # one-off calls) — null means "no registered prompt drove this".
    prompt_id: str | None = Field(default=None, min_length=1, max_length=128)
    prompt_version: int | None = Field(default=None, ge=1)
    prompt_excerpt: str | None = Field(
        default=None,
        description="Redacted, bounded preview of the request messages sent to the model",
    )
    # Phase β+ step-kind tag for multi-model routing aggregation. Free
    # short string ("plan" / "edit" / "review" / "chat" / "observe");
    # null when the caller did not classify the step.
    step_kind: str | None = Field(default=None, min_length=1, max_length=32)

    cost_usd_micros: int | None = Field(default=None, ge=0)
    latency_ms: int = Field(..., ge=0)

    status: LLMUsageStatus
    error_category: ErrorCategory | None = None
    error_message: str | None = Field(
        default=None,
        description="Short, redacted error summary; never the full prompt or response body",
    )

    created_at: datetime
