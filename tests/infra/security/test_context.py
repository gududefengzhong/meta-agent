"""Unit tests for RequestContext propagation."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from meta_agent.core.domain import AgentError, ErrorCategory
from meta_agent.infra.security import (
    MissingContextError,
    RequestContext,
    bind_context,
    get_current,
    require_current,
    require_tenant_id,
    update_context,
)


def _sample_ctx(**overrides: object) -> RequestContext:
    base: dict[str, object] = {
        "tenant_id": "t-1",
        "principal_id": "p-1",
        "trace_id": "trace-1",
        "request_id": "req-1",
    }
    base.update(overrides)
    return RequestContext(**base)  # type: ignore[arg-type]


def test_get_current_returns_none_when_unbound() -> None:
    assert get_current() is None


def test_require_current_raises_when_unbound() -> None:
    with pytest.raises(MissingContextError) as exc:
        require_current()
    assert isinstance(exc.value, AgentError)
    assert exc.value.category is ErrorCategory.LOGIC
    assert exc.value.retryable is False


def test_bind_context_sets_and_restores() -> None:
    ctx = _sample_ctx()
    assert get_current() is None
    with bind_context(ctx) as bound:
        assert bound is ctx
        assert get_current() is ctx
        assert require_tenant_id() == "t-1"
    assert get_current() is None


def test_bind_context_nests() -> None:
    outer = _sample_ctx(tenant_id="outer")
    inner = _sample_ctx(tenant_id="inner")
    with bind_context(outer):
        assert require_tenant_id() == "outer"
        with bind_context(inner):
            assert require_tenant_id() == "inner"
        assert require_tenant_id() == "outer"


def test_context_is_frozen() -> None:
    ctx = _sample_ctx()
    with pytest.raises(FrozenInstanceError):
        ctx.tenant_id = "other"  # type: ignore[misc]


def test_update_context_replaces_fields_only() -> None:
    base = _sample_ctx()
    with bind_context(base):
        with update_context(task_id="task-1", session_id="sess-1") as bound:
            assert bound.task_id == "task-1"
            assert bound.session_id == "sess-1"
            assert bound.tenant_id == "t-1"
            assert bound.trace_id == "trace-1"
        # After update_context exits, outer binding restored.
        assert get_current() is base


def test_update_context_requires_existing_binding() -> None:
    with pytest.raises(MissingContextError):  # noqa: SIM117
        with update_context(task_id="task-1"):
            pass


async def test_context_propagates_across_await() -> None:
    ctx = _sample_ctx(tenant_id="async-tenant")

    async def inner() -> str:
        await asyncio.sleep(0)
        return require_tenant_id()

    with bind_context(ctx):
        result = await inner()
    assert result == "async-tenant"


async def test_context_propagates_to_spawned_tasks() -> None:
    ctx = _sample_ctx(tenant_id="task-tenant")

    async def inner() -> str:
        await asyncio.sleep(0)
        return require_tenant_id()

    with bind_context(ctx):
        task = asyncio.create_task(inner())
        result = await task
    assert result == "task-tenant"


async def test_concurrent_tasks_are_isolated() -> None:
    async def run_with(tenant: str) -> str:
        with bind_context(_sample_ctx(tenant_id=tenant)):
            await asyncio.sleep(0)
            return require_tenant_id()

    results = await asyncio.gather(run_with("a"), run_with("b"), run_with("c"))
    # ``asyncio.gather`` is typed as a tuple but returns a list at runtime;
    # cast to a list for a stable comparison across Python versions.
    assert list(results) == ["a", "b", "c"]
