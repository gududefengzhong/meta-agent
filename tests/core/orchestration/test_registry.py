"""Unit tests for :class:`GraphRegistry`."""

from __future__ import annotations

import pytest

from meta_agent.core.domain.task import TaskType
from meta_agent.core.orchestration import (
    END,
    Graph,
    GraphDeps,
    GraphError,
    GraphFactory,
    GraphRegistry,
    NodeResult,
    TaskRunState,
)
from tests.core.orchestration._fakes import fake_deps


async def _noop(state: TaskRunState) -> NodeResult:
    return NodeResult()


def _graph_factory(graph_id: str, *, build_id: str | None = None) -> GraphFactory:
    """Return a factory that produces a one-node graph with ``build_id``.

    ``build_id`` defaults to ``graph_id`` and lets tests force a
    mismatch between the registration key and the factory's output.
    """

    actual_id = build_id if build_id is not None else graph_id

    def factory(_deps: GraphDeps) -> Graph:
        g = Graph(actual_id)
        g.add_node("only", _noop)
        g.set_entry("only")
        g.add_edge("only", END)
        return g

    return factory


def test_register_and_get_by_id_after_materialize() -> None:
    reg = GraphRegistry()
    reg.register("g.a", _graph_factory("g.a"))
    reg.materialize(fake_deps())
    assert reg.get("g.a").graph_id == "g.a"


def test_get_before_materialize_raises() -> None:
    reg = GraphRegistry()
    reg.register("g.a", _graph_factory("g.a"))
    with pytest.raises(GraphError):
        reg.get("g.a")


def test_resolve_before_materialize_raises() -> None:
    reg = GraphRegistry()
    reg.register("g.a", _graph_factory("g.a"), default_for=TaskType.BUG_FIX)
    with pytest.raises(GraphError):
        reg.resolve(TaskType.BUG_FIX)


def test_materialize_validates_factory_returns_matching_graph_id() -> None:
    reg = GraphRegistry()
    reg.register("g.a", _graph_factory("g.a", build_id="g.b"))
    with pytest.raises(GraphError):
        reg.materialize(fake_deps())


def test_materialize_compiles_graphs() -> None:
    reg = GraphRegistry()

    def bad_factory(_deps: GraphDeps) -> Graph:
        return Graph("g.bad")  # no entry, no edges → compile() must fail

    reg.register("g.bad", bad_factory)
    with pytest.raises(GraphError):
        reg.materialize(fake_deps())


def test_resolve_uses_explicit_graph_id() -> None:
    reg = GraphRegistry()
    reg.register("g.a", _graph_factory("g.a"), default_for=TaskType.BUG_FIX)
    reg.register("g.b", _graph_factory("g.b"))
    reg.materialize(fake_deps())
    resolved = reg.resolve(TaskType.BUG_FIX, graph_id="g.b")
    assert resolved.graph_id == "g.b"


def test_resolve_falls_back_to_task_type_default() -> None:
    reg = GraphRegistry()
    reg.register("g.echo", _graph_factory("g.echo"), default_for=TaskType.SYSTEM_ECHO)
    reg.materialize(fake_deps())
    resolved = reg.resolve(TaskType.SYSTEM_ECHO)
    assert resolved.graph_id == "g.echo"


def test_resolve_raises_when_no_default() -> None:
    reg = GraphRegistry()
    reg.register("g.a", _graph_factory("g.a"))
    reg.materialize(fake_deps())
    with pytest.raises(GraphError):
        reg.resolve(TaskType.BUG_FIX)


def test_get_unknown_raises() -> None:
    reg = GraphRegistry()
    reg.materialize(fake_deps())
    with pytest.raises(GraphError):
        reg.get("missing")


def test_duplicate_registration_rejected() -> None:
    reg = GraphRegistry()
    reg.register("g.a", _graph_factory("g.a"))
    with pytest.raises(GraphError):
        reg.register("g.a", _graph_factory("g.a"))


def test_register_after_materialize_rejected() -> None:
    reg = GraphRegistry()
    reg.register("g.a", _graph_factory("g.a"))
    reg.materialize(fake_deps())
    with pytest.raises(GraphError):
        reg.register("g.b", _graph_factory("g.b"))


def test_materialize_twice_rejected() -> None:
    reg = GraphRegistry()
    reg.register("g.a", _graph_factory("g.a"))
    reg.materialize(fake_deps())
    with pytest.raises(GraphError):
        reg.materialize(fake_deps())


def test_duplicate_default_rejected() -> None:
    reg = GraphRegistry()
    reg.register("g.a", _graph_factory("g.a"), default_for=TaskType.BUG_FIX)
    with pytest.raises(GraphError):
        reg.register("g.b", _graph_factory("g.b"), default_for=TaskType.BUG_FIX)


def test_empty_graph_id_rejected() -> None:
    reg = GraphRegistry()
    with pytest.raises(GraphError):
        reg.register("", _graph_factory("g.a"))


def test_default_graph_id_lookup_before_materialize() -> None:
    reg = GraphRegistry()
    reg.register("g.echo", _graph_factory("g.echo"), default_for=TaskType.SYSTEM_ECHO)
    assert reg.default_graph_id(TaskType.SYSTEM_ECHO) == "g.echo"
    assert reg.default_graph_id(TaskType.BUG_FIX) is None


def test_is_materialized_flag() -> None:
    reg = GraphRegistry()
    assert reg.is_materialized is False
    reg.register("g.a", _graph_factory("g.a"))
    assert reg.is_materialized is False
    reg.materialize(fake_deps())
    assert reg.is_materialized is True


def test_requires_workspace_flag_tracks_registration() -> None:
    reg = GraphRegistry()
    reg.register("g.plain", _graph_factory("g.plain"))
    reg.register("g.ws", _graph_factory("g.ws"), requires_workspace=True)
    assert reg.requires_workspace("g.plain") is False
    assert reg.requires_workspace("g.ws") is True
    # Unknown graph_id reads as not-required rather than raising; the
    # worker uses this to gate provisioning, not to validate identity.
    assert reg.requires_workspace("g.unknown") is False
