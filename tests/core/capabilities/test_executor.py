"""Unit tests for :class:`ToolExecutor`."""

from __future__ import annotations

import pytest

from meta_agent.core.capabilities.executor import ToolExecutor
from meta_agent.core.capabilities.registry import ToolRegistry
from meta_agent.core.ports.tools import (
    ToolCall,
    ToolCategory,
    ToolContext,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
)


def _spec(name: str = "echo") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="d",
        parameters={"type": "object"},
        category=ToolCategory.FILESYSTEM,
    )


def _ctx(*, output_byte_cap: int = 65536) -> ToolContext:
    return ToolContext(
        tenant_id="t",
        task_id="task",
        trace_id="trace",
        output_byte_cap=output_byte_cap,
    )


def _call(name: str = "echo", call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name=name)


async def test_unknown_tool_short_circuits_to_is_error() -> None:
    executor = ToolExecutor(ToolRegistry())
    result = await executor.execute(_call("missing"), _ctx())
    assert result.is_error is True
    assert result.call_id == "c1"
    assert result.name == "missing"
    assert "missing" in result.content


async def test_handler_tool_error_normalised_to_is_error_result() -> None:
    registry = ToolRegistry()

    async def boom(call: ToolCall, ctx: ToolContext) -> ToolResult:
        raise ToolExecutionError("kaboom")

    registry.register(_spec("boom"), boom)
    executor = ToolExecutor(registry)
    result = await executor.execute(_call("boom"), _ctx())
    assert result.is_error is True
    assert "kaboom" in result.content


async def test_unexpected_exception_propagates() -> None:
    registry = ToolRegistry()

    async def crash(call: ToolCall, ctx: ToolContext) -> ToolResult:
        raise RuntimeError("not a ToolError")

    registry.register(_spec("crash"), crash)
    executor = ToolExecutor(registry)
    with pytest.raises(RuntimeError):
        await executor.execute(_call("crash"), _ctx())


async def test_truncates_to_executor_cap_on_success() -> None:
    registry = ToolRegistry()

    async def big(call: ToolCall, ctx: ToolContext) -> ToolResult:
        return ToolResult(call_id=call.id, name=call.name, content="a" * 100)

    registry.register(_spec("big"), big)
    executor = ToolExecutor(registry, max_result_bytes=10)
    result = await executor.execute(_call("big"), _ctx())
    assert len(result.content.encode("utf-8")) == 10
    assert result.truncated is True


async def test_per_call_cap_overrides_when_smaller() -> None:
    registry = ToolRegistry()

    async def big(call: ToolCall, ctx: ToolContext) -> ToolResult:
        return ToolResult(call_id=call.id, name=call.name, content="b" * 100)

    registry.register(_spec("big"), big)
    executor = ToolExecutor(registry, max_result_bytes=80)
    result = await executor.execute(_call("big"), _ctx(output_byte_cap=5))
    assert len(result.content.encode("utf-8")) == 5
    assert result.truncated is True


async def test_truncation_preserves_utf8_boundaries() -> None:
    registry = ToolRegistry()

    async def emoji(call: ToolCall, ctx: ToolContext) -> ToolResult:
        # "😀" is 4 bytes in UTF-8; with a 5-byte cap we must keep one emoji
        # plus zero partial bytes, not an invalid 5-byte slice.
        return ToolResult(call_id=call.id, name=call.name, content="😀😀😀")

    registry.register(_spec("emoji"), emoji)
    executor = ToolExecutor(registry, max_result_bytes=5)
    result = await executor.execute(_call("emoji"), _ctx())
    assert result.content == "😀"
    assert result.truncated is True


async def test_success_below_cap_returns_handler_result_unchanged() -> None:
    registry = ToolRegistry()
    sentinel = ToolResult(call_id="c1", name="ok", content="hi", metadata={"k": "v"})

    async def ok(call: ToolCall, ctx: ToolContext) -> ToolResult:
        return sentinel

    registry.register(_spec("ok"), ok)
    executor = ToolExecutor(registry)
    result = await executor.execute(_call("ok"), _ctx())
    assert result is sentinel  # untouched fast-path
    assert result.metadata == {"k": "v"}


def test_executor_rejects_non_positive_cap() -> None:
    with pytest.raises(ValueError):
        ToolExecutor(ToolRegistry(), max_result_bytes=0)
