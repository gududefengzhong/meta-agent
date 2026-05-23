"""Inline permission protocol domain types (Phase δ-1).

Different mechanism than the γ-A async ``AWAITING_APPROVAL``
workflow:

* γ-A: task state transitions to ``AWAITING_APPROVAL``, worker
  releases resources, operator visits a separate UI / API, the
  approval flows back via a different channel, task resumes minutes
  to hours later. Right for high-stakes long-running async reviews
  (PR-style sign-off).
* δ-1 inline: the agent is mid-loop, hits a sensitive action,
  emits a prompt to the connected client, blocks for *seconds* on
  a decision, then continues. Right for interactive code-agent UX
  (VS Code / CLI users asked "run ``rm -rf foo/``? [allow/deny]").

The two coexist — operators pick per ``PermissionMode``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

PermissionAction = Literal["allow", "deny"]


class PermissionPrompt(BaseModel):
    """One inline ask-the-user-now request emitted by the worker.

    Carries enough context for a client to render a sensible UI
    without round-tripping the API for more metadata:

    * ``tool_name`` + ``summary`` — what the agent wants to do
    * ``payload`` — JSON-shaped tool arguments (already redacted by
      :class:`RedactingLLMClient`), so the client can show the
      details that drove the decision
    * ``prompt_id`` — opaque identifier the client uses to POST the
      decision back to /v1/tasks/{task_id}/permissions/{prompt_id}/decide

    Frozen so producers can pass the same instance to multiple
    consumers (audit emission, broadcaster, in-process gate) without
    accidental mutation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    task_id: str = Field(..., min_length=1)
    tool_name: str = Field(..., min_length=1)
    summary: str = Field(default="", description="Human-readable one-liner")
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class PermissionDecision(BaseModel):
    """Client's response to a :class:`PermissionPrompt`.

    ``allow=True`` means the agent should proceed with the proposed
    action exactly as described in the prompt. ``allow=False`` means
    the agent should skip the action and (typically) tell the model
    so it can plan an alternative.

    ``reason`` is an optional free-text note from the user; surfaced
    in audit rows + fed back to the model when ``allow=False`` so
    the agent understands *why* the action was rejected.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_id: str = Field(..., min_length=1)
    allow: bool
    reason: str | None = None
    decided_at: datetime


__all__ = ["PermissionAction", "PermissionDecision", "PermissionPrompt"]
