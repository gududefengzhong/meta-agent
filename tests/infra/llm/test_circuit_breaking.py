"""Unit tests for :class:`CircuitBreakingLLMClient`.

Cover the observable wrapper behaviours:

* allow path forwards to inner unchanged and computes the right key
* :class:`CircuitBreakerOpenError` is translated to
  :class:`LLMTransientError` (LLM port contract preserved)
* :class:`CircuitBreakerBackendError` fails open (default) or propagates
* ``should_count`` default excludes caller-side LLM errors so they do
  not trip the breaker
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

import pytest

from meta_agent.core.ports.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerBackendError,
    CircuitBreakerOpenError,
)
from meta_agent.core.ports.llm import (
    ChatMessage,
    LLMAuthError,
    LLMInvalidRequestError,
    LLMRequest,
    LLMTransientError,
    MessageRole,
)
from meta_agent.infra.llm.circuit_breaking import (
    CircuitBreakingLLMClient,
    _default_should_count,
)
from meta_agent.infra.security.context import RequestContext, bind_context
from tests.core.orchestration._fakes import FakeLLMClient

T = TypeVar("T")


class _ScriptedBreaker(CircuitBreaker):
    """Records keys + ``should_count`` predicates; emits scripted outcomes."""

    def __init__(
        self,
        *,
        outcomes: list[CircuitBreakerOpenError | CircuitBreakerBackendError] | None = None,
    ) -> None:
        self._outcomes = list(outcomes or [])
        self.keys: list[str] = []
        self.predicates: list[Callable[[BaseException], bool] | None] = []
        self.closed = False

    async def call(
        self,
        key: str,
        fn: Callable[[], Awaitable[T]],
        *,
        should_count: Callable[[BaseException], bool] | None = None,
    ) -> T:
        self.keys.append(key)
        self.predicates.append(should_count)
        if self._outcomes:
            raise self._outcomes.pop(0)
        return await fn()

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


async def test_allow_path_forwards_to_inner_with_namespaced_key() -> None:
    inner = FakeLLMClient()
    breaker = _ScriptedBreaker()
    client = CircuitBreakingLLMClient(inner, breaker, provider="openrouter")
    with bind_context(_ctx()):
        response = await client.complete(_request())
    assert response.content == "ok"
    assert len(inner.calls) == 1
    assert breaker.keys == ["llm:openrouter:tenant=t-1:model=openai/gpt-4o"]


async def test_open_translates_to_llm_transient_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    inner = FakeLLMClient()
    breaker = _ScriptedBreaker(
        outcomes=[CircuitBreakerOpenError("open", key="k", retry_after_ms=500)],
    )
    client = CircuitBreakingLLMClient(inner, breaker, provider="openrouter")
    with (
        bind_context(_ctx()),
        caplog.at_level(logging.INFO, logger="meta_agent.infra.llm.circuit_breaking"),
        pytest.raises(LLMTransientError),
    ):
        await client.complete(_request())
    assert inner.calls == []
    assert any("circuit_breaker.open" in rec.getMessage() for rec in caplog.records)


async def test_backend_error_fails_open_by_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    inner = FakeLLMClient()
    breaker = _ScriptedBreaker(outcomes=[CircuitBreakerBackendError("redis blip")])
    client = CircuitBreakingLLMClient(inner, breaker, provider="openrouter")
    with (
        bind_context(_ctx()),
        caplog.at_level(logging.WARNING, logger="meta_agent.infra.llm.circuit_breaking"),
    ):
        response = await client.complete(_request())
    assert response.content == "ok"
    assert len(inner.calls) == 1
    assert any("backend_error_fail_open" in rec.getMessage() for rec in caplog.records)


async def test_backend_error_propagates_when_fail_closed() -> None:
    inner = FakeLLMClient()
    breaker = _ScriptedBreaker(outcomes=[CircuitBreakerBackendError("redis blip")])
    client = CircuitBreakingLLMClient(inner, breaker, provider="openrouter", fail_open=False)
    with bind_context(_ctx()), pytest.raises(CircuitBreakerBackendError):
        await client.complete(_request())
    assert inner.calls == []


async def test_default_should_count_excludes_caller_side_errors() -> None:
    # The decorator passes its predicate down to breaker.call; verify
    # both that the predicate is installed and that it excludes the
    # two caller-side categories.
    inner = FakeLLMClient()
    breaker = _ScriptedBreaker()
    client = CircuitBreakingLLMClient(inner, breaker, provider="openrouter")
    with bind_context(_ctx()):
        await client.complete(_request())
    predicate = breaker.predicates[0]
    assert predicate is not None
    assert predicate(LLMInvalidRequestError("bad")) is False
    assert predicate(LLMAuthError("auth")) is False
    assert predicate(LLMTransientError("net")) is True
    assert predicate(RuntimeError("oops")) is True


def test_default_should_count_module_level_helper() -> None:
    assert _default_should_count(LLMInvalidRequestError("x")) is False
    assert _default_should_count(LLMAuthError("x")) is False
    assert _default_should_count(RuntimeError("x")) is True


async def test_missing_context_uses_anonymous_label() -> None:
    inner = FakeLLMClient()
    breaker = _ScriptedBreaker()
    client = CircuitBreakingLLMClient(inner, breaker, provider="openrouter")
    await client.complete(_request())
    assert breaker.keys == ["llm:openrouter:tenant=anonymous:model=openai/gpt-4o"]


async def test_missing_model_uses_default_label() -> None:
    inner = FakeLLMClient()
    breaker = _ScriptedBreaker()
    client = CircuitBreakingLLMClient(inner, breaker, provider="openrouter")
    with bind_context(_ctx()):
        await client.complete(_request(model=None))
    assert breaker.keys == ["llm:openrouter:tenant=t-1:model=default"]


async def test_close_delegates_to_inner() -> None:
    inner = FakeLLMClient()
    breaker = _ScriptedBreaker()
    client = CircuitBreakingLLMClient(inner, breaker, provider="openrouter")
    await client.close()
    assert inner.closed is True


def test_construction_rejects_empty_provider() -> None:
    with pytest.raises(ValueError, match="provider must be a non-empty string"):
        CircuitBreakingLLMClient(FakeLLMClient(), _ScriptedBreaker(), provider="")
