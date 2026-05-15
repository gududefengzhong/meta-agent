"""Unit tests for :class:`GraphRegistry`."""

from __future__ import annotations

import pytest

from meta_agent.core.domain.task import TaskType
from meta_agent.core.orchestration import (
    END,
    Graph,
    GraphError,
    GraphRegistry,
    NodeResult,
    TaskRunState,
)


async def _noop(state: TaskRunState) -> NodeResult:
    return NodeResult()


def _graph(graph_id: str) -> Graph:
    g = Graph(graph_id)
    g.add_node("only", _noop)
    g.set_entry("only")
    g.add_edge("only", END)
    return g


def test_register_and_get_by_id() -> None:
    reg = GraphRegistry()
    g = _graph("g.a")
    reg.register(g)
    assert reg.get("g.a") is g


def test_register_compiles_graph() -> None:
    reg = GraphRegistry()
    bad = Graph("g.bad")  # no entry, no edges → must fail to compile
    with pytest.raises(GraphError):
        reg.register(bad)


def test_resolve_uses_explicit_graph_id() -> None:
    reg = GraphRegistry()
    reg.register(_graph("g.a"), default_for=TaskType.BUG_FIX)
    reg.register(_graph("g.b"))
    resolved = reg.resolve(TaskType.BUG_FIX, graph_id="g.b")
    assert resolved.graph_id == "g.b"


def test_resolve_falls_back_to_task_type_default() -> None:
    reg = GraphRegistry()
    reg.register(_graph("g.echo"), default_for=TaskType.SYSTEM_ECHO)
    resolved = reg.resolve(TaskType.SYSTEM_ECHO)
    assert resolved.graph_id == "g.echo"


def test_resolve_raises_when_no_default() -> None:
    reg = GraphRegistry()
    reg.register(_graph("g.a"))
    with pytest.raises(GraphError):
        reg.resolve(TaskType.BUG_FIX)


def test_get_unknown_raises() -> None:
    reg = GraphRegistry()
    with pytest.raises(GraphError):
        reg.get("missing")


def test_duplicate_registration_rejected() -> None:
    reg = GraphRegistry()
    reg.register(_graph("g.a"))
    with pytest.raises(GraphError):
        reg.register(_graph("g.a"))


def test_duplicate_default_rejected() -> None:
    reg = GraphRegistry()
    reg.register(_graph("g.a"), default_for=TaskType.BUG_FIX)
    with pytest.raises(GraphError):
        reg.register(_graph("g.b"), default_for=TaskType.BUG_FIX)


def test_default_graph_id_lookup() -> None:
    reg = GraphRegistry()
    reg.register(_graph("g.echo"), default_for=TaskType.SYSTEM_ECHO)
    assert reg.default_graph_id(TaskType.SYSTEM_ECHO) == "g.echo"
    assert reg.default_graph_id(TaskType.BUG_FIX) is None
