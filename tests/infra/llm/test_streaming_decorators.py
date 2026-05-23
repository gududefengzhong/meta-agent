"""Streaming-path tests for all LLM decorators.

Each decorator already has a full :meth:`complete` suite in its own
test file. These tests target the :meth:`stream` override exclusively:

* every decorator forwards to ``inner.stream`` (not ``inner.complete``)
* pre-flight gates (rate-limit, budget, circuit breaker) raise
  *before* the first chunk is yielded — never mid-stream
* :class:`MeteredLLMClient` aggregates chunk deltas into one
  :class:`LLMUsageRecord` instead of one row per chunk
* :class:`RedactingLLMClient` scrubs per-chunk (best-effort per the
  documented streaming caveat)
* the full production decorator stack streams end-to-end so a missed
  ``stream`` override surfaces as a test failure rather than a silent
  fallback to the buffered default implementation
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import TypeVar

import pytest

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.llm_usage import LLMUsageRecord, LLMUsageStatus
from meta_agent.core.ports.audit_sink import AuditSink
from meta_agent.core.ports.budget import (
    BudgetDecision,
    BudgetEnforcer,
    BudgetUsage,
)
from meta_agent.core.ports.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
)
from meta_agent.core.ports.llm import (
    ChatMessage,
    LLMBudgetExceededError,
    LLMClient,
    LLMRateLimitedError,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    LLMTransientError,
    LLMUsage,
    MessageRole,
    ToolCallDelta,
)
from meta_agent.core.ports.llm_router import LLMRouter
from meta_agent.core.ports.llm_usage import (
    LLMUsageFilter,
    LLMUsageRepository,
    UsageAggregate,
    UsageGroupBy,
)
from meta_agent.core.ports.rate_limiter import (
    RateLimitDecision,
    RateLimiter,
)
from meta_agent.infra.llm.budget_enforcing import BudgetEnforcingLLMClient
from meta_agent.infra.llm.circuit_breaking import CircuitBreakingLLMClient
from meta_agent.infra.llm.metered import MeteredLLMClient
from meta_agent.infra.llm.rate_limited import RateLimitedLLMClient
from meta_agent.infra.llm.redacting import RedactingLLMClient
from meta_agent.infra.llm.routing import RoutingLLMClient
from meta_agent.infra.redaction.redactor import Redactor
from meta_agent.infra.security.context import RequestContext, bind_context

T = TypeVar("T")


# ----------------------------------------------------------- fakes / fixtures


class _RecordingAuditSink(AuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> None:
        self.events.append(event)


class _StreamingFake(LLMClient):
    """Inner :class:`LLMClient` that records calls and yields canned chunks.

    Crucially, :meth:`complete` raises so any decorator that
    accidentally drops back to the default buffered ``stream``
    implementation (which calls ``complete``) fails loudly.
    """

    def __init__(self, chunks: list[LLMStreamChunk]) -> None:
        self._chunks = list(chunks)
        self.stream_calls: list[LLMRequest] = []
        self.complete_calls: list[LLMRequest] = []
        self.closed = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.complete_calls.append(request)
        raise AssertionError("inner.complete must not be invoked during stream tests")

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        self.stream_calls.append(request)
        for chunk in self._chunks:
            yield chunk

    async def close(self) -> None:
        self.closed = True


class _ScriptedLimiter(RateLimiter):
    def __init__(self, decision: RateLimitDecision) -> None:
        self._decision = decision
        self.keys: list[str] = []
        self.closed = False

    async def acquire(self, key: str, *, cost: int = 1) -> RateLimitDecision:
        self.keys.append(key)
        return self._decision

    async def close(self) -> None:
        self.closed = True


class _ScriptedEnforcer(BudgetEnforcer):
    def __init__(self, decision: BudgetDecision) -> None:
        self._decision = decision
        self.tenants: list[str] = []
        self.closed = False

    async def check(self, tenant_id: str) -> BudgetDecision:
        self.tenants.append(tenant_id)
        return self._decision

    async def close(self) -> None:
        self.closed = True


class _StaticRouter(LLMRouter):
    def __init__(self, target: str | None) -> None:
        self._target = target

    async def select_model(self, *, step_kind: str, tenant_id: str | None = None) -> str | None:
        return self._target


class _NeverOpenBreaker(CircuitBreaker):
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    async def call(
        self,
        key: str,
        fn: Callable[[], Awaitable[T]],
        *,
        should_count: Callable[[BaseException], bool] | None = None,
    ) -> T:
        self.calls += 1
        return await fn()

    async def close(self) -> None:
        self.closed = True


class _AlwaysOpenBreaker(CircuitBreaker):
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False

    async def call(
        self,
        key: str,
        fn: Callable[[], Awaitable[T]],
        *,
        should_count: Callable[[BaseException], bool] | None = None,
    ) -> T:
        self.calls += 1
        raise CircuitBreakerOpenError("open", key=key, retry_after_ms=200)

    async def close(self) -> None:
        self.closed = True


class _StubUsageRepository(LLMUsageRepository):
    """Records LLMUsageRecord writes; no read paths exercised here."""

    def __init__(self) -> None:
        self.records: list[LLMUsageRecord] = []

    async def record(self, record: LLMUsageRecord) -> None:
        self.records.append(record)

    async def list_for_task(self, tenant_id: str, task_id: str) -> list[LLMUsageRecord]:
        raise AssertionError("list_for_task not exercised by streaming tests")

    async def aggregate_since(self, tenant_id: str, since: datetime) -> BudgetUsage:
        raise AssertionError("aggregate_since not exercised by streaming tests")

    async def list_filtered(self, tenant_id: str, filt: LLMUsageFilter) -> list[LLMUsageRecord]:
        raise AssertionError("list_filtered not exercised by streaming tests")

    async def aggregate_grouped(
        self,
        tenant_id: str,
        since: datetime,
        until: datetime,
        group_by: UsageGroupBy,
    ) -> list[UsageAggregate]:
        raise AssertionError("aggregate_grouped not exercised by streaming tests")

    async def aggregate_for_task(
        self,
        tenant_id: str,
        task_id: str,
        group_by: UsageGroupBy,
    ) -> list[UsageAggregate]:
        raise AssertionError("aggregate_for_task not exercised by streaming tests")


def _ctx(tenant_id: str = "t-1") -> RequestContext:
    return RequestContext(
        tenant_id=tenant_id,
        principal_id="p-1",
        trace_id="trace-1",
        request_id="req-1",
        task_id="task-1",
    )


def _request(model: str | None = "openai/gpt-4o", step_kind: str | None = None) -> LLMRequest:
    return LLMRequest(
        messages=(ChatMessage(role=MessageRole.USER, content="hi"),),
        model=model,
        step_kind=step_kind,
    )


def _ok_chunks() -> list[LLMStreamChunk]:
    return [
        LLMStreamChunk(content_delta="he"),
        LLMStreamChunk(content_delta="llo"),
        LLMStreamChunk(
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=3, completion_tokens=5, total_tokens=8),
            model="openai/gpt-4o",
            provider_response_id="gen_abc",
        ),
    ]


async def _drain(client: LLMClient, request: LLMRequest) -> list[LLMStreamChunk]:
    chunks: list[LLMStreamChunk] = []
    async for chunk in client.stream(request):
        chunks.append(chunk)
    return chunks


# --------------------------------------------------------- routing pass-through


async def test_routing_stream_rewrites_model_and_forwards_to_inner_stream() -> None:
    inner = _StreamingFake(_ok_chunks())
    router = _StaticRouter("deepseek/deepseek-chat")
    client = RoutingLLMClient(inner, router)
    chunks = await _drain(client, _request(step_kind="planning"))
    assert "".join(c.content_delta for c in chunks) == "hello"
    assert len(inner.stream_calls) == 1
    assert inner.stream_calls[0].model == "deepseek/deepseek-chat"
    assert inner.complete_calls == []


async def test_routing_stream_passes_through_when_router_returns_none() -> None:
    inner = _StreamingFake(_ok_chunks())
    router = _StaticRouter(None)
    client = RoutingLLMClient(inner, router)
    await _drain(client, _request(step_kind="planning"))
    assert inner.stream_calls[0].model == "openai/gpt-4o"


# --------------------------------------------------- rate-limited pre-check


async def test_rate_limited_stream_denied_before_any_chunk_yielded() -> None:
    inner = _StreamingFake(_ok_chunks())
    limiter = _ScriptedLimiter(RateLimitDecision(allowed=False, remaining=0, retry_after_ms=750))
    audit = _RecordingAuditSink()
    client = RateLimitedLLMClient(inner, limiter, provider="openrouter", audit_sink=audit)
    with bind_context(_ctx()), pytest.raises(LLMRateLimitedError) as excinfo:
        await _drain(client, _request())
    assert excinfo.value.retry_after == 0.75
    assert inner.stream_calls == []
    deny_events = [e for e in audit.events if e.action == "llm.rate_limited.denied"]
    assert len(deny_events) == 1


async def test_rate_limited_stream_allowed_forwards_to_inner_stream() -> None:
    inner = _StreamingFake(_ok_chunks())
    limiter = _ScriptedLimiter(RateLimitDecision(allowed=True, remaining=99))
    client = RateLimitedLLMClient(inner, limiter, provider="openrouter")
    with bind_context(_ctx()):
        chunks = await _drain(client, _request())
    assert "".join(c.content_delta for c in chunks) == "hello"
    assert len(inner.stream_calls) == 1


# --------------------------------------------------- budget-enforcing pre-check


async def test_budget_stream_denied_before_any_chunk_yielded() -> None:
    inner = _StreamingFake(_ok_chunks())
    enforcer = _ScriptedEnforcer(
        BudgetDecision(
            allowed=False,
            usage=BudgetUsage(tokens_used=10_000, cost_usd_micros_used=0),
            limit_tokens=5_000,
        )
    )
    client = BudgetEnforcingLLMClient(inner, enforcer, provider="openrouter", cache_ttl_s=0)
    with bind_context(_ctx()), pytest.raises(LLMBudgetExceededError):
        await _drain(client, _request())
    assert inner.stream_calls == []


async def test_budget_stream_allowed_forwards_to_inner_stream() -> None:
    inner = _StreamingFake(_ok_chunks())
    enforcer = _ScriptedEnforcer(
        BudgetDecision(
            allowed=True,
            usage=BudgetUsage(tokens_used=10, cost_usd_micros_used=0),
            limit_tokens=5_000,
        )
    )
    client = BudgetEnforcingLLMClient(inner, enforcer, provider="openrouter", cache_ttl_s=0)
    with bind_context(_ctx()):
        chunks = await _drain(client, _request())
    assert "".join(c.content_delta for c in chunks) == "hello"
    assert len(inner.stream_calls) == 1


# ------------------------------------------------ circuit-breaking gate


async def test_circuit_breaking_stream_open_raises_without_iterating() -> None:
    inner = _StreamingFake(_ok_chunks())
    breaker = _AlwaysOpenBreaker()
    audit = _RecordingAuditSink()
    client = CircuitBreakingLLMClient(inner, breaker, provider="openrouter", audit_sink=audit)
    with bind_context(_ctx()), pytest.raises(LLMTransientError):
        await _drain(client, _request())
    open_events = [e for e in audit.events if e.action == "llm.circuit_breaker.open"]
    assert len(open_events) == 1


async def test_circuit_breaking_stream_closed_forwards_full_stream() -> None:
    inner = _StreamingFake(_ok_chunks())
    breaker = _NeverOpenBreaker()
    client = CircuitBreakingLLMClient(inner, breaker, provider="openrouter")
    with bind_context(_ctx()):
        chunks = await _drain(client, _request())
    assert "".join(c.content_delta for c in chunks) == "hello"
    assert breaker.calls == 1  # the first-chunk fetch was breaker-guarded
    assert len(inner.stream_calls) == 1


# ------------------------------------------- metered single-row aggregation


async def test_metered_stream_records_single_usage_row_with_aggregated_tokens() -> None:
    chunks = [
        LLMStreamChunk(content_delta="he"),
        LLMStreamChunk(content_delta="llo"),
        LLMStreamChunk(
            tool_call_deltas=(
                ToolCallDelta(index=0, id="call_1", name="fs_read", arguments_delta='{"pa'),
            )
        ),
        LLMStreamChunk(tool_call_deltas=(ToolCallDelta(index=0, arguments_delta='th":"a.py"}'),)),
        LLMStreamChunk(
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=3, completion_tokens=7, total_tokens=10),
            model="openai/gpt-4o",
            provider_response_id="gen_abc",
        ),
    ]
    inner = _StreamingFake(chunks)
    recorder = _StubUsageRepository()
    client = MeteredLLMClient(
        inner,
        recorder,
        provider="openrouter",
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        record_id_factory=lambda: "llmu-test",
    )
    with bind_context(_ctx()):
        emitted = await _drain(client, _request())
    assert len(emitted) == 5  # passthrough preserved
    assert len(recorder.records) == 1
    row = recorder.records[0]
    assert row.status is LLMUsageStatus.OK
    assert row.prompt_tokens == 3
    assert row.completion_tokens == 7
    assert row.total_tokens == 10
    assert row.finish_reason == "stop"
    assert row.model == "openai/gpt-4o"
    assert row.provider_response_id == "gen_abc"


async def test_metered_stream_error_mid_stream_records_error_row_and_reraises() -> None:
    class _ExplodingFake(LLMClient):
        async def complete(self, request: LLMRequest) -> LLMResponse:
            raise AssertionError("not used")

        async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
            yield LLMStreamChunk(content_delta="par")
            raise LLMTransientError("upstream blew up mid-stream")

        async def close(self) -> None:
            pass

    recorder = _StubUsageRepository()
    client = MeteredLLMClient(
        _ExplodingFake(),
        recorder,
        provider="openrouter",
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    with bind_context(_ctx()), pytest.raises(LLMTransientError):
        await _drain(client, _request())
    assert len(recorder.records) == 1
    row = recorder.records[0]
    assert row.status is LLMUsageStatus.ERROR
    assert row.error_message == "upstream blew up mid-stream"


# -------------------------------------------- redacting per-chunk pass


async def test_redacting_stream_scrubs_each_chunk() -> None:
    secret = "ghp_" + "a" * 40
    chunks = [
        LLMStreamChunk(content_delta="here is "),
        LLMStreamChunk(content_delta=f"a token: {secret}"),
        LLMStreamChunk(finish_reason="stop"),
    ]
    inner = _StreamingFake(chunks)
    audit = _RecordingAuditSink()
    client = RedactingLLMClient(inner, redactor=Redactor(), audit_sink=audit)
    with bind_context(_ctx()):
        out = await _drain(client, _request())
    joined = "".join(c.content_delta for c in out)
    assert secret not in joined
    assert "[REDACTED:github_token]" in joined
    response_audits = [e for e in audit.events if e.action == "llm.redaction.applied_to_response"]
    assert len(response_audits) == 1


# ----------------------------------------- full-stack streaming end-to-end


async def test_full_decorator_stack_streams_without_falling_back_to_complete() -> None:
    """Wire all six decorators in production order and stream end-to-end.

    The base fake's ``complete`` raises; if any decorator omits its
    ``stream`` override the inherited default impl calls ``complete``
    and the test fails. Catches regressions where a new decorator
    forgets to forward stream semantics.
    """

    inner = _StreamingFake(_ok_chunks())
    breaker_wrap = CircuitBreakingLLMClient(inner, _NeverOpenBreaker(), provider="openrouter")
    metered_wrap = MeteredLLMClient(breaker_wrap, _StubUsageRepository(), provider="openrouter")
    rl_wrap = RateLimitedLLMClient(
        metered_wrap,
        _ScriptedLimiter(RateLimitDecision(allowed=True, remaining=99)),
        provider="openrouter",
    )
    be_wrap = BudgetEnforcingLLMClient(
        rl_wrap,
        _ScriptedEnforcer(
            BudgetDecision(
                allowed=True,
                usage=BudgetUsage(tokens_used=0, cost_usd_micros_used=0),
                limit_tokens=None,
            )
        ),
        provider="openrouter",
        cache_ttl_s=0,
    )
    router_wrap = RoutingLLMClient(be_wrap, _StaticRouter("deepseek/deepseek-chat"))
    top = RedactingLLMClient(router_wrap, redactor=Redactor())

    with bind_context(_ctx()):
        chunks = await _drain(top, _request(step_kind="planning"))

    assert "".join(c.content_delta for c in chunks) == "hello"
    assert len(inner.stream_calls) == 1
    assert inner.complete_calls == []
    assert inner.stream_calls[0].model == "deepseek/deepseek-chat"
