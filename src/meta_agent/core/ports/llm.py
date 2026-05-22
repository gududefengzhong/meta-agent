"""LLM client port.

The orchestration core depends only on this port; concrete adapters
(OpenRouter, Anthropic, ...) live under :mod:`meta_agent.infra.llm`.
Keeping the port narrow means graph nodes can be unit-tested against
in-memory fakes without ever touching the network.

Error taxonomy reuses :class:`meta_agent.core.domain.errors.AgentError`
so retry policy decisions stay consistent with the rest of the
platform.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from meta_agent.core.domain.errors import AgentError, ErrorCategory
from meta_agent.core.ports.tools import ToolCall, ToolSpec


class MessageRole(StrEnum):
    """OpenAI-style chat role taxonomy.

    ``TOOL`` carries the observation returned to the LLM after a
    tool invocation; it MUST appear with a populated
    :attr:`ChatMessage.tool_call_id` so the model can correlate the
    result with the original :class:`ToolCall` it emitted.
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ChatMessage(BaseModel):
    """A single turn in a chat completion request.

    ``tool_call_id`` is required on ``role=TOOL`` messages (the
    correlation id of the originating :class:`ToolCall`). ``tool_calls``
    is only meaningful on ``role=ASSISTANT`` messages that emitted one
    or more tool calls; ``content`` may be an empty string in that case
    because the assistant turn is "function-only".
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: MessageRole
    content: str
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


class LLMUsage(BaseModel):
    """Token accounting returned by the provider.

    Adapters may report ``None`` for unknown fields when the upstream
    response does not include usage data; callers should treat missing
    counts as "unknown" rather than zero for billing purposes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class LLMRequest(BaseModel):
    """Provider-agnostic chat-completion request.

    ``model`` is an opaque provider-specific identifier (for OpenRouter
    this is the ``provider/name`` slug). Adapters validate it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: tuple[ChatMessage, ...] = Field(min_length=1)
    model: str | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, gt=0)
    stop: tuple[str, ...] | None = None
    tools: tuple[ToolSpec, ...] = ()
    """Optional tool catalogue advertised to the upstream model.

    Empty tuple means a plain chat completion (no tool-use surface).
    Adapters forward the specs verbatim as OpenAI-style ``tools`` array;
    the LLM may respond with :attr:`LLMResponse.tool_calls` instead of
    a final assistant message.
    """
    metadata: dict[str, str] = Field(default_factory=dict)
    """Free-form labels propagated to provider headers when supported.

    Intended for ``X-Title`` / ``HTTP-Referer`` style attribution, not
    for secrets — adapters must refuse to forward any key matching
    ``*_token`` / ``*_key`` patterns to avoid accidental leakage.
    """
    prompt_id: str | None = Field(default=None, min_length=1, max_length=128)
    """Identifier of the prompt asset that produced ``messages[0..]``.

    Phase β+ provenance: when a graph resolves its system / user prompts
    through :class:`PromptRegistry`, it MUST set ``prompt_id`` (and
    ``prompt_version`` below) on the outgoing request so
    ``llm_usage_logs`` can join each call back to the exact registered
    template that drove it. Ad-hoc callers that compose messages by
    hand leave both fields ``None``.
    """
    prompt_version: int | None = Field(default=None, ge=1)
    """Version of the prompt asset referenced by ``prompt_id``."""
    step_kind: str | None = Field(default=None, min_length=1, max_length=32)
    """Coarse classification of the step that triggered the call.

    Phase β+ multi-model routing: graphs tag each LLM call with one of
    a small vocabulary (``"plan"`` / ``"edit"`` / ``"review"`` /
    ``"chat"`` / ``"observe"``). A :class:`LLMRouter` decorator at the
    top of the LLM stack inspects this tag and may override
    :attr:`model`; the :class:`MeteredLLMClient` records the tag so
    ``llm_usage_logs`` can aggregate by step kind for cost / quality
    A/B analysis. Ad-hoc callers leave it ``None``.
    """


FinishReason = Literal["stop", "length", "content_filter", "tool_call", "other"]


class LLMResponse(BaseModel):
    """Provider-agnostic chat-completion response.

    ``tool_calls`` is non-empty exactly when the model elected to invoke
    one or more tools instead of (or alongside) emitting assistant text;
    ``finish_reason`` will typically be ``"tool_call"`` in that case but
    callers should branch on ``tool_calls`` rather than the reason.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str
    model: str
    finish_reason: FinishReason
    usage: LLMUsage = Field(default_factory=LLMUsage)
    tool_calls: tuple[ToolCall, ...] = ()
    provider_response_id: str | None = None
    """Upstream response ID, useful when correlating against vendor logs."""


class LLMError(AgentError):
    """Base class for adapter-raised LLM errors.

    Default category is :class:`ErrorCategory.EXTERNAL`; transient
    subclasses override it so the orchestration layer can decide to
    retry without parsing the exception class.
    """

    category = ErrorCategory.EXTERNAL


class LLMTransientError(LLMError):
    """Recoverable failure (timeout, 5xx, transient network error)."""

    category = ErrorCategory.TRANSIENT


class LLMRateLimitedError(LLMTransientError):
    """HTTP 429 or provider-signalled rate limiting.

    Adapters surface ``retry_after`` when the upstream supplies it so
    callers can honour backoff hints without re-parsing headers.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMInvalidRequestError(LLMError):
    """4xx error caused by malformed/forbidden caller input. Not retryable."""

    category = ErrorCategory.VALIDATION


class LLMAuthError(LLMError):
    """401/403 from the upstream. Indicates a missing or revoked key."""

    category = ErrorCategory.PERMISSION


class LLMBudgetExceededError(LLMError):
    """Raised when the caller's monthly LLM budget is exhausted.

    Distinct from :class:`LLMRateLimitedError`: a rate-limit deny clears
    on the next refill window (typically seconds); a budget deny clears
    on the next billing window (typically end-of-month). Retrying is
    almost never appropriate, so this is a :class:`ErrorCategory.PERMISSION`
    error rather than :class:`ErrorCategory.TRANSIENT`.
    """

    category = ErrorCategory.PERMISSION

    def __init__(
        self,
        message: str,
        *,
        tokens_used: int | None = None,
        limit_tokens: int | None = None,
    ) -> None:
        super().__init__(message)
        self.tokens_used = tokens_used
        self.limit_tokens = limit_tokens


class LLMClient(ABC):
    """Adapter contract: turn a typed request into a typed response."""

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Run a chat completion. Must raise an :class:`LLMError` on failure."""

    @abstractmethod
    async def close(self) -> None:
        """Release any underlying connection pool. Safe to call multiple times."""
