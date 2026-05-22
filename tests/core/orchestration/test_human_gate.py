"""Unit tests for :func:`build_human_gate` and the AWAITING_APPROVAL graph pause."""

from __future__ import annotations

import pytest

from meta_agent.core.orchestration import (
    END,
    HUMAN_DECISION_KEY,
    HUMAN_FEEDBACK_KEY,
    HUMAN_GATE_AT_KEY,
    Graph,
    GraphError,
    NodeResult,
    TaskRunState,
    build_human_gate,
)


def _state(**data: object) -> TaskRunState:
    return TaskRunState(
        task_id="task-1",
        tenant_id="tenant-1",
        trace_id="trace-1",
        graph_id="builtin.test",
        data=data,
    )


async def test_gate_pauses_when_no_decision_present() -> None:
    gate = build_human_gate(gate_id="before_push", next_node_when_approved="push")
    result = await gate(_state())
    assert result.awaiting_approval is True
    assert result.next_node is None
    assert result.data_update[HUMAN_GATE_AT_KEY] == "before_push"


async def test_gate_advances_when_approved() -> None:
    gate = build_human_gate(gate_id="before_push", next_node_when_approved="push")
    result = await gate(_state(_human_decision="approve", _human_feedback="ok"))
    assert result.awaiting_approval is False
    assert result.next_node == "push"
    # Decision is consumed so a later gate does not see a stale value.
    assert result.data_update[HUMAN_DECISION_KEY] is None
    # Gate location is cleared so trajectory queries do not show a
    # stale "still at this gate" marker.
    assert result.data_update[HUMAN_GATE_AT_KEY] is None


async def test_gate_routes_to_end_on_reject() -> None:
    gate = build_human_gate(gate_id="before_push", next_node_when_approved="push")
    result = await gate(_state(_human_decision="reject"))
    assert result.next_node == END
    assert result.data_update["_rejected_by_human"] is True


async def test_gate_re_pauses_on_unknown_decision_string() -> None:
    """An unrecognised decision string must NOT silently advance.

    Operator API validation prevents this in practice, but the gate is
    the defence in depth — a future tool that writes
    ``_human_decision="maybe"`` should not advance the graph past
    the gate.
    """

    gate = build_human_gate(gate_id="g1", next_node_when_approved="push")
    result = await gate(_state(_human_decision="maybe"))
    assert result.awaiting_approval is True
    assert result.next_node is None


async def test_gate_factory_rejects_empty_args() -> None:
    with pytest.raises(GraphError, match="gate_id"):
        build_human_gate(gate_id="", next_node_when_approved="push")
    with pytest.raises(GraphError, match="next_node_when_approved"):
        build_human_gate(gate_id="g1", next_node_when_approved="")


# ---------------------------------------------------------------------------
# Graph runtime tests: how the AWAITING_APPROVAL signal flows through Graph.
# ---------------------------------------------------------------------------


async def test_graph_pauses_at_gate_without_advancing_current_node() -> None:
    g = Graph("builtin.test")

    async def producer(_state: TaskRunState) -> NodeResult:
        return NodeResult(data_update={"k": "v"})

    g.add_node("producer", producer)
    g.add_node("gate", build_human_gate(gate_id="g1", next_node_when_approved="finalize"))
    g.add_node("finalize", _noop_finalize)
    g.set_entry("producer")
    g.add_edge("producer", "gate")
    g.add_edge("gate", "finalize")
    g.add_edge("finalize", END)
    g.compile()

    final = await g.run(_state())
    # The run loop stops at the gate. ``current_node`` still points at
    # the gate (so a resume re-executes it), ``awaiting_approval`` is
    # set, ``finished`` is false.
    assert final.awaiting_approval is True
    assert final.finished is False
    assert final.current_node == "gate"
    # The state already collected the producer's data + the gate's
    # marker about which gate paused us.
    assert final.data["k"] == "v"
    assert final.data[HUMAN_GATE_AT_KEY] == "g1"


async def test_graph_resumes_past_gate_after_decision_injected() -> None:
    g = Graph("builtin.test")
    finalize_calls: list[str] = []

    async def producer(_state: TaskRunState) -> NodeResult:
        return NodeResult()

    async def finalize(_state: TaskRunState) -> NodeResult:
        finalize_calls.append("ran")
        return NodeResult()

    g.add_node("producer", producer)
    g.add_node("gate", build_human_gate(gate_id="g1", next_node_when_approved="finalize"))
    g.add_node("finalize", finalize)
    g.set_entry("producer")
    g.add_edge("producer", "gate")
    g.add_edge("gate", "finalize")
    g.add_edge("finalize", END)
    g.compile()

    paused = await g.run(_state())
    # Inject the decision as the approval gateway would in production.
    resumed_state = paused.model_copy(
        update={
            "data": {**paused.data, HUMAN_DECISION_KEY: "approve", HUMAN_FEEDBACK_KEY: "ok"},
            "awaiting_approval": False,
        }
    )
    final = await g.run(resumed_state)
    assert final.finished is True
    assert finalize_calls == ["ran"]


async def test_step_is_no_op_when_state_already_paused() -> None:
    g = Graph("builtin.test")
    g.add_node("gate", build_human_gate(gate_id="g1", next_node_when_approved="finalize"))
    g.add_node("finalize", _noop_finalize)
    g.set_entry("gate")
    g.add_edge("gate", "finalize")
    g.add_edge("finalize", END)
    g.compile()

    paused = await g.run(_state())
    same = await g.step(paused)
    assert same is paused  # short-circuit per Graph.step contract


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _noop_finalize(_state: TaskRunState) -> NodeResult:
    return NodeResult(next_node=END)
