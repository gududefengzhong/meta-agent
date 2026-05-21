"""Unit tests for :class:`ToolRegistry`."""

from __future__ import annotations

import pytest

from meta_agent.core.capabilities.registry import ToolRegistry
from meta_agent.core.ports.tools import (
    ToolCall,
    ToolCategory,
    ToolContext,
    ToolNotFoundError,
    ToolResult,
    ToolSpec,
    ToolValidationError,
)


def _spec(name: str = "noop") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="d",
        parameters={"type": "object"},
        category=ToolCategory.FILESYSTEM,
    )


async def _handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
    return ToolResult(call_id=call.id, name=call.name, content="ok")


def test_register_then_get_returns_same_handler() -> None:
    registry = ToolRegistry()
    spec = _spec("a")
    registry.register(spec, _handler)
    rt = registry.get("a")
    assert rt.spec is spec
    assert rt.handler is _handler


def test_duplicate_registration_raises_validation_error() -> None:
    registry = ToolRegistry()
    registry.register(_spec("dup"), _handler)
    with pytest.raises(ToolValidationError):
        registry.register(_spec("dup"), _handler)


def test_get_unknown_name_raises_not_found() -> None:
    registry = ToolRegistry()
    with pytest.raises(ToolNotFoundError):
        registry.get("missing")


def test_list_specs_is_sorted_by_name() -> None:
    registry = ToolRegistry()
    registry.register(_spec("z"), _handler)
    registry.register(_spec("a"), _handler)
    registry.register(_spec("m"), _handler)
    specs = registry.list_specs()
    assert [s.name for s in specs] == ["a", "m", "z"]


def test_names_and_contains_membership() -> None:
    registry = ToolRegistry()
    registry.register(_spec("x"), _handler)
    assert registry.names() == frozenset({"x"})
    assert "x" in registry
    assert "y" not in registry
    assert 42 not in registry  # type: ignore[operator]


def test_len_reflects_population() -> None:
    registry = ToolRegistry()
    assert len(registry) == 0
    registry.register(_spec("a"), _handler)
    registry.register(_spec("b"), _handler)
    assert len(registry) == 2
