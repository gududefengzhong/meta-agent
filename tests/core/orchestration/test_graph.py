"""Unit tests for the minimal graph runtime."""

from __future__ import annotations

import pytest

from meta_agent.core.orchestration import (
    END,
    Graph,
    GraphError,
    NodeResult,
    TaskRunState,
)


def _state(**overrides: object) -> TaskRunState:
    base: dict[str, object] = {
        "task_id": "task-1",
        "tenant_id": "t-1",
        "trace_id": "trace-1",
        "graph_id": "g",
    }
    base.update(overrides)
    return TaskRunState(**base)


async def _noop(state: TaskRunState) -> NodeResult:
    return NodeResult()


@pytest.mark.asyncio
async def test_graph_runs_linear_chain() -> None:
    g = Graph("g")

    async def plan(state: TaskRunState) -> NodeResult:
        return NodeResult(data_update={"plan": "p"})

    async def act(state: TaskRunState) -> NodeResult:
        plan = str(state.data["plan"])
        return NodeResult(data_update={"act": plan + "+a"})

    g.add_node("plan", plan)
    g.add_node("act", act)
    g.set_entry("plan")
    g.add_edge("plan", "act")
    g.add_edge("act", END)
    g.compile()

    final = await g.run(_state())
    assert final.finished is True
    assert final.current_node == END
    assert final.sequence == 2
    assert final.data == {"plan": "p", "act": "p+a"}


@pytest.mark.asyncio
async def test_graph_router_picks_destination() -> None:
    g = Graph("g")

    async def gate(state: TaskRunState) -> NodeResult:
        return NodeResult(data_update={"branch": state.data["pick"]})

    async def left(state: TaskRunState) -> NodeResult:
        return NodeResult(data_update={"path": "L"})

    async def right(state: TaskRunState) -> NodeResult:
        return NodeResult(data_update={"path": "R"})

    g.add_node("gate", gate)
    g.add_node("left", left)
    g.add_node("right", right)
    g.set_entry("gate")
    g.add_conditional("gate", lambda s: str(s.data["branch"]))
    g.add_edge("left", END)
    g.add_edge("right", END)
    g.compile()

    final = await g.run(_state(data={"pick": "left"}))
    assert final.data["path"] == "L"


@pytest.mark.asyncio
async def test_node_override_takes_precedence_over_edge() -> None:
    g = Graph("g")

    async def a(state: TaskRunState) -> NodeResult:
        return NodeResult(next_node=END)

    g.add_node("a", a)
    g.set_entry("a")
    g.add_edge("a", "a")
    g.compile()

    final = await g.run(_state())
    assert final.current_node == END


@pytest.mark.asyncio
async def test_step_on_finished_state_is_noop() -> None:
    g = Graph("g")
    g.add_node("a", _noop)
    g.set_entry("a")
    g.add_edge("a", END)
    g.compile()

    finished = await g.run(_state())
    again = await g.step(finished)
    assert again is finished


@pytest.mark.asyncio
async def test_run_respects_max_steps() -> None:
    g = Graph("g")

    async def loop(state: TaskRunState) -> NodeResult:
        return NodeResult()

    g.add_node("loop", loop)
    g.set_entry("loop")
    g.add_edge("loop", "loop")
    g.compile()

    with pytest.raises(GraphError):
        await g.run(_state(), max_steps=3)


def test_compile_rejects_node_without_outgoing_edge() -> None:
    g = Graph("g")
    g.add_node("a", _noop)
    g.set_entry("a")
    with pytest.raises(GraphError):
        g.compile()


def test_compile_rejects_missing_entry() -> None:
    g = Graph("g")
    g.add_node("a", _noop)
    g.add_edge("a", END)
    with pytest.raises(GraphError):
        g.compile()


def test_add_edge_to_unknown_destination_raises() -> None:
    g = Graph("g")
    g.add_node("a", _noop)
    with pytest.raises(GraphError):
        g.add_edge("a", "ghost")


def test_add_node_rejects_reserved_names() -> None:
    g = Graph("g")
    with pytest.raises(GraphError):
        g.add_node(END, _noop)


def test_duplicate_node_rejected() -> None:
    g = Graph("g")
    g.add_node("a", _noop)
    with pytest.raises(GraphError):
        g.add_node("a", _noop)


def test_duplicate_edge_rejected() -> None:
    g = Graph("g")
    g.add_node("a", _noop)
    g.add_edge("a", END)
    with pytest.raises(GraphError):
        g.add_edge("a", END)


@pytest.mark.asyncio
async def test_router_returning_unknown_node_raises() -> None:
    g = Graph("g")
    g.add_node("a", _noop)
    g.set_entry("a")
    g.add_conditional("a", lambda s: "ghost")
    g.compile()
    with pytest.raises(GraphError):
        await g.run(_state())
