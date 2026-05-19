"""Unit tests for :class:`RateLimitedLLMClient`.

Cover the four observable behaviours:

* allow path forwards to inner unchanged
* deny path raises :class:`LLMRateLimitedError` carrying the retry hint
* backend errors fail open (default) or propagate (``fail_open=False``)
* key derivation embeds ``tenant_id`` and ``model`` so multi-tenant
  buckets stay isolated
"""

from __future__ import annotations

import logging

import pytest

from meta_agent.core.ports.llm import (
    ChatMessage,
    LLMRateLimitedError,
    LLMRequest,
    MessageRole,
)
from meta_agent.core.ports.rate_limiter import (
    RateLimitDecision,
    RateLimiter,
    RateLimiterBackendError,
)
from meta_agent.infra.llm.rate_limited import RateLimitedLLMClient
from meta_agent.infra.security.context import RequestContext, bind_context
from tests.core.orchestration._fakes import FakeLLMClient


class _ScriptedLimiter(RateLimiter):
    """Records every key seen; emits scripted decisions / errors."""

    def __init__(
        self,
        *,
        outcomes: list[RateLimitDecision | RateLimiterBackendError] | None = None,
        default: RateLimitDecision | None = None,
    ) -> None:
        self._outcomes = list(outcomes or [])
        self._default = default or RateLimitDecision(allowed=True, remaining=999)
        self.keys: list[str] = []
        self.closed = False

    async def acquire(self, key: str, *, cost: int = 1) -> RateLimitDecision:
        self.keys.append(key)
        if not self._outcomes:
            return self._default
        nxt = self._outcomes.pop(0)
        if isinstance(nxt, RateLimiterBackendError):
            raise nxt
        return nxt

    async def close(self) -> None:
        self.closed = True


def _ctx(tenant_id: str = "t-1") -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id,
        principal_id="p-1",
        trace_id="trace-1",
        request_id="req-1",
        task_id="task-1",
    )


def _request(model: str | None = "openai/gpt-4o") -> LLMRequest:
    return LLMRequest(
        messages=(ChatMessage(role=MessageRole.USER, content="hi"),),
        model=model,
    )


async def test_allow_path_forwards_to_inner() -> None:
    inner = FakeLLMClient()
    limiter = _ScriptedLimiter()
    client = RateLimitedLLMClient(inner, limiter, provider="openrouter")
    with bind_context(_ctx()):
        response = await client.complete(_request())
    assert response.content == "ok"
    assert len(inner.calls) == 1
    assert limiter.keys == ["llm:openrouter:tenant=t-1:model=openai/gpt-4o"]


async def test_deny_path_raises_with_retry_hint_and_skips_inner() -> None:
    inner = FakeLLMClient()
    limiter = _ScriptedLimiter(
        outcomes=[RateLimitDecision(allowed=False, remaining=0, retry_after_ms=750)],
    )
    client = RateLimitedLLMClient(inner, limiter, provider="openrouter")
    with bind_context(_ctx()), pytest.raises(LLMRateLimitedError) as excinfo:
        await client.complete(_request())
    assert excinfo.value.retry_after == 0.75
    assert inner.calls == []  # inner must NOT be invoked when denied


async def test_key_embeds_tenant_and_model() -> None:
    inner = FakeLLMClient()
    limiter = _ScriptedLimiter()
    client = RateLimitedLLMClient(inner, limiter, provider="openrouter")
    with bind_context(_ctx("tenant-a")):
        await client.complete(_request("anthropic/claude-3-5"))
    with bind_context(_ctx("tenant-b")):
        await client.complete(_request("openai/gpt-4o"))
    assert limiter.keys == [
        "llm:openrouter:tenant=tenant-a:model=anthropic/claude-3-5",
        "llm:openrouter:tenant=tenant-b:model=openai/gpt-4o",
    ]


async def test_missing_context_uses_anonymous_label() -> None:
    inner = FakeLLMClient()
    limiter = _ScriptedLimiter()
    client = RateLimitedLLMClient(inner, limiter, provider="openrouter")
    await client.complete(_request())
    assert limiter.keys == ["llm:openrouter:tenant=anonymous:model=openai/gpt-4o"]


async def test_missing_model_uses_default_label() -> None:
    inner = FakeLLMClient()
    limiter = _ScriptedLimiter()
    client = RateLimitedLLMClient(inner, limiter, provider="openrouter")
    with bind_context(_ctx()):
        await client.complete(_request(model=None))
    assert limiter.keys == ["llm:openrouter:tenant=t-1:model=default"]


async def test_backend_error_fails_open_by_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    inner = FakeLLMClient()
    limiter = _ScriptedLimiter(outcomes=[RateLimiterBackendError("redis down")])
    client = RateLimitedLLMClient(inner, limiter, provider="openrouter")
    with (
        bind_context(_ctx()),
        caplog.at_level(logging.WARNING, logger="meta_agent.infra.llm.rate_limited"),
    ):
        response = await client.complete(_request())
    assert response.content == "ok"
    assert len(inner.calls) == 1
    assert any("backend_error_fail_open" in rec.getMessage() for rec in caplog.records)


async def test_backend_error_propagates_when_fail_closed() -> None:
    inner = FakeLLMClient()
    limiter = _ScriptedLimiter(outcomes=[RateLimiterBackendError("redis down")])
    client = RateLimitedLLMClient(inner, limiter, provider="openrouter", fail_open=False)
    with bind_context(_ctx()), pytest.raises(RateLimiterBackendError):
        await client.complete(_request())
    assert inner.calls == []


async def test_close_delegates_to_inner() -> None:
    inner = FakeLLMClient()
    limiter = _ScriptedLimiter()
    client = RateLimitedLLMClient(inner, limiter, provider="openrouter")
    await client.close()
    assert inner.closed is True


def test_construction_rejects_empty_provider() -> None:
    with pytest.raises(ValueError, match="provider must be a non-empty string"):
        RateLimitedLLMClient(FakeLLMClient(), _ScriptedLimiter(), provider="")
