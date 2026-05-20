"""Unit tests for :class:`BudgetEnforcingLLMClient`."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.ports.audit_sink import AuditSink
from meta_agent.core.ports.budget import (
    BudgetBackendError,
    BudgetDecision,
    BudgetEnforcer,
    BudgetUsage,
)
from meta_agent.core.ports.llm import (
    ChatMessage,
    LLMBudgetExceededError,
    LLMRequest,
    MessageRole,
)
from meta_agent.infra.llm.budget_enforcing import BudgetEnforcingLLMClient
from meta_agent.infra.security.context import RequestContext, bind_context
from tests.core.orchestration._fakes import FakeLLMClient


class _RecordingAuditSink(AuditSink):
    def __init__(self, *, raise_on_append: BaseException | None = None) -> None:
        self.events: list[AuditEvent] = []
        self._raise = raise_on_append

    async def append(self, event: AuditEvent) -> None:
        if self._raise is not None:
            raise self._raise
        self.events.append(event)


class _ScriptedEnforcer(BudgetEnforcer):
    def __init__(
        self,
        *,
        outcomes: list[BudgetDecision | BudgetBackendError] | None = None,
        default: BudgetDecision | None = None,
    ) -> None:
        self._outcomes = list(outcomes or [])
        self._default = default or BudgetDecision(
            allowed=True,
            usage=BudgetUsage(tokens_used=0, cost_usd_micros_used=0),
            limit_tokens=None,
        )
        self.tenants: list[str] = []
        self.closed = False

    async def check(self, tenant_id: str) -> BudgetDecision:
        self.tenants.append(tenant_id)
        if not self._outcomes:
            return self._default
        nxt = self._outcomes.pop(0)
        if isinstance(nxt, BudgetBackendError):
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


def _denied(tokens: int = 100, limit: int = 50) -> BudgetDecision:
    return BudgetDecision(
        allowed=False,
        usage=BudgetUsage(tokens_used=tokens, cost_usd_micros_used=0),
        limit_tokens=limit,
    )


async def test_allow_path_forwards_to_inner() -> None:
    inner = FakeLLMClient()
    enforcer = _ScriptedEnforcer()
    client = BudgetEnforcingLLMClient(inner, enforcer, provider="openrouter")
    with bind_context(_ctx()):
        response = await client.complete(_request())
    assert response.content == "ok"
    assert len(inner.calls) == 1
    assert enforcer.tenants == ["t-1"]


async def test_deny_path_raises_and_skips_inner() -> None:
    inner = FakeLLMClient()
    enforcer = _ScriptedEnforcer(outcomes=[_denied(100, 50)])
    client = BudgetEnforcingLLMClient(inner, enforcer, provider="openrouter")
    with bind_context(_ctx()), pytest.raises(LLMBudgetExceededError) as excinfo:
        await client.complete(_request())
    assert excinfo.value.tokens_used == 100
    assert excinfo.value.limit_tokens == 50
    assert inner.calls == []


async def test_cache_short_circuits_within_ttl() -> None:
    inner = FakeLLMClient()
    enforcer = _ScriptedEnforcer()
    ticks = iter([0.0, 1.0, 5.0])
    client = BudgetEnforcingLLMClient(
        inner,
        enforcer,
        provider="openrouter",
        cache_ttl_s=10.0,
        monotonic=lambda: next(ticks),
    )
    with bind_context(_ctx()):
        await client.complete(_request())
        await client.complete(_request())
        await client.complete(_request())
    # Only the first call should reach the enforcer.
    assert enforcer.tenants == ["t-1"]
    assert len(inner.calls) == 3


async def test_cache_expires_after_ttl() -> None:
    inner = FakeLLMClient()
    enforcer = _ScriptedEnforcer()
    # Insert / read pattern: 0 (insert), 5 (hit), 11 (expired ⇒ refresh, insert)
    ticks = iter([0.0, 5.0, 11.0])
    client = BudgetEnforcingLLMClient(
        inner,
        enforcer,
        provider="openrouter",
        cache_ttl_s=10.0,
        monotonic=lambda: next(ticks),
    )
    with bind_context(_ctx()):
        await client.complete(_request())
        await client.complete(_request())
        await client.complete(_request())
    assert enforcer.tenants == ["t-1", "t-1"]


async def test_cache_disabled_when_ttl_zero() -> None:
    inner = FakeLLMClient()
    enforcer = _ScriptedEnforcer()
    client = BudgetEnforcingLLMClient(inner, enforcer, provider="openrouter", cache_ttl_s=0.0)
    with bind_context(_ctx()):
        await client.complete(_request())
        await client.complete(_request())
    assert enforcer.tenants == ["t-1", "t-1"]


async def test_cache_isolates_per_tenant() -> None:
    inner = FakeLLMClient()
    enforcer = _ScriptedEnforcer()
    ticks = iter([0.0, 0.0])
    client = BudgetEnforcingLLMClient(
        inner,
        enforcer,
        provider="openrouter",
        cache_ttl_s=10.0,
        monotonic=lambda: next(ticks),
    )
    with bind_context(_ctx("tenant-a")):
        await client.complete(_request())
    with bind_context(_ctx("tenant-b")):
        await client.complete(_request())
    assert enforcer.tenants == ["tenant-a", "tenant-b"]


async def test_backend_error_fails_open_by_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    inner = FakeLLMClient()
    enforcer = _ScriptedEnforcer(outcomes=[BudgetBackendError("db down")])
    client = BudgetEnforcingLLMClient(inner, enforcer, provider="openrouter")
    with (
        bind_context(_ctx()),
        caplog.at_level(logging.WARNING, logger="meta_agent.infra.llm.budget_enforcing"),
    ):
        response = await client.complete(_request())
    assert response.content == "ok"
    assert any("backend_error_fail_open" in rec.getMessage() for rec in caplog.records)


async def test_backend_error_propagates_when_fail_closed() -> None:
    inner = FakeLLMClient()
    enforcer = _ScriptedEnforcer(outcomes=[BudgetBackendError("db down")])
    client = BudgetEnforcingLLMClient(inner, enforcer, provider="openrouter", fail_open=False)
    with bind_context(_ctx()), pytest.raises(BudgetBackendError):
        await client.complete(_request())
    assert inner.calls == []


async def test_backend_error_is_not_cached() -> None:
    """A transient backend error must not poison the cache."""

    inner = FakeLLMClient()
    enforcer = _ScriptedEnforcer(
        outcomes=[
            BudgetBackendError("db blip"),
            BudgetDecision(
                allowed=True,
                usage=BudgetUsage(tokens_used=0, cost_usd_micros_used=0),
                limit_tokens=None,
            ),
        ]
    )
    ticks = iter([0.0, 1.0])
    client = BudgetEnforcingLLMClient(
        inner,
        enforcer,
        provider="openrouter",
        cache_ttl_s=10.0,
        monotonic=lambda: next(ticks),
    )
    with bind_context(_ctx()):
        await client.complete(_request())  # fails open, no cache write
        await client.complete(_request())  # second check still hits enforcer
    assert enforcer.tenants == ["t-1", "t-1"]


async def test_missing_context_bypasses_enforcement(
    caplog: pytest.LogCaptureFixture,
) -> None:
    inner = FakeLLMClient()
    enforcer = _ScriptedEnforcer(outcomes=[_denied()])
    client = BudgetEnforcingLLMClient(inner, enforcer, provider="openrouter")
    with caplog.at_level(logging.DEBUG, logger="meta_agent.infra.llm.budget_enforcing"):
        response = await client.complete(_request())
    assert response.content == "ok"
    # Enforcer never consulted; the denied outcome stays queued.
    assert enforcer.tenants == []


async def test_deny_emits_audit_event_with_payload() -> None:
    inner = FakeLLMClient()
    enforcer = _ScriptedEnforcer(outcomes=[_denied(tokens=123, limit=100)])
    sink = _RecordingAuditSink()
    fixed = datetime(2025, 1, 1, tzinfo=UTC)
    client = BudgetEnforcingLLMClient(
        inner,
        enforcer,
        provider="openrouter",
        audit_sink=sink,
        clock=lambda: fixed,
        event_id_factory=lambda: "ae-fixed",
    )
    with bind_context(_ctx()), pytest.raises(LLMBudgetExceededError):
        await client.complete(_request())

    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.event_id == "ae-fixed"
    assert event.action == "llm.budget.exceeded"
    assert event.tenant_id == "t-1"
    assert event.principal_id == "p-1"
    assert event.trace_id == "trace-1"
    assert event.task_id == "task-1"
    assert event.occurred_at == fixed
    assert event.payload == {
        "provider": "openrouter",
        "requested_model": "openai/gpt-4o",
        "tokens_used": 123,
        "cost_usd_micros_used": 0,
        "limit_tokens": 100,
    }


async def test_allow_path_does_not_emit_audit() -> None:
    inner = FakeLLMClient()
    enforcer = _ScriptedEnforcer()
    sink = _RecordingAuditSink()
    client = BudgetEnforcingLLMClient(inner, enforcer, provider="openrouter", audit_sink=sink)
    with bind_context(_ctx()):
        await client.complete(_request())
    assert sink.events == []


async def test_audit_append_error_is_swallowed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    inner = FakeLLMClient()
    enforcer = _ScriptedEnforcer(outcomes=[_denied()])
    sink = _RecordingAuditSink(raise_on_append=RuntimeError("audit table down"))
    client = BudgetEnforcingLLMClient(inner, enforcer, provider="openrouter", audit_sink=sink)
    with (
        bind_context(_ctx()),
        caplog.at_level(logging.WARNING, logger="meta_agent.infra.llm.budget_enforcing"),
        pytest.raises(LLMBudgetExceededError),
    ):
        await client.complete(_request())
    assert any("audit_append_failed" in rec.getMessage() for rec in caplog.records)


async def test_close_delegates_to_inner() -> None:
    inner = FakeLLMClient()
    enforcer = _ScriptedEnforcer()
    client = BudgetEnforcingLLMClient(inner, enforcer, provider="openrouter")
    await client.close()
    assert inner.closed is True


def test_construction_rejects_empty_provider() -> None:
    with pytest.raises(ValueError, match="provider must be a non-empty string"):
        BudgetEnforcingLLMClient(FakeLLMClient(), _ScriptedEnforcer(), provider="")


def test_construction_rejects_negative_ttl() -> None:
    with pytest.raises(ValueError, match="cache_ttl_s must be >= 0"):
        BudgetEnforcingLLMClient(
            FakeLLMClient(),
            _ScriptedEnforcer(),
            provider="openrouter",
            cache_ttl_s=-1.0,
        )
