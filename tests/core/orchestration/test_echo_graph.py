"""Unit tests for the built-in echo graph."""

from __future__ import annotations

from meta_agent.core.orchestration import END, TaskRunState
from meta_agent.core.orchestration.graphs import ECHO_GRAPH_ID, build_echo_graph


def _state(message: str = "hello") -> TaskRunState:
    return TaskRunState(
        task_id="task-1",
        tenant_id="t-1",
        trace_id="trace-1",
        graph_id=ECHO_GRAPH_ID,
        data={"message": message},
    )


async def test_echo_graph_produces_three_step_transcript() -> None:
    g = build_echo_graph()
    final = await g.run(_state("hi"))
    assert final.current_node == END
    assert final.finished is True
    assert final.sequence == 3
    transcript = final.data["transcript"]
    assert isinstance(transcript, list)
    assert transcript == [
        "plan: received 'hi'",
        "execute: echo 'hi'",
        "review: ok",
    ]
    assert final.data["output"] == "hi"


async def test_echo_graph_id_is_stable() -> None:
    assert ECHO_GRAPH_ID == "builtin.echo"
    assert build_echo_graph().graph_id == ECHO_GRAPH_ID


async def test_echo_graph_round_trips_state_via_checkpoint_shape() -> None:
    g = build_echo_graph()
    final = await g.run(_state())
    dumped = final.model_dump(mode="json")
    restored = TaskRunState.model_validate(dumped)
    assert restored == final
