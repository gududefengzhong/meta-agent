"""Built-in echo graph: a deterministic smoke-test flow.

The graph has three nodes — ``plan`` → ``execute`` → ``review`` — that
read a ``message`` from the initial state ``data`` and accumulate a
``transcript`` list. It performs no I/O and has no LLM dependency, so
it can be used to validate the worker, checkpoint, audit and queue
plumbing end-to-end without involving an external provider.
"""

from __future__ import annotations

from meta_agent.core.orchestration.graph import Graph, NodeResult
from meta_agent.core.orchestration.state import END, TaskRunState

ECHO_GRAPH_ID = "builtin.echo"


def _message(state: TaskRunState) -> str:
    raw = state.data.get("message", "")
    return raw if isinstance(raw, str) else str(raw)


def _transcript(state: TaskRunState) -> list[str]:
    raw = state.data.get("transcript", [])
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


async def _plan(state: TaskRunState) -> NodeResult:
    line = f"plan: received {_message(state)!r}"
    return NodeResult(data_update={"transcript": [*_transcript(state), line]})


async def _execute(state: TaskRunState) -> NodeResult:
    line = f"execute: echo {_message(state)!r}"
    return NodeResult(
        data_update={
            "transcript": [*_transcript(state), line],
            "output": _message(state),
        }
    )


async def _review(state: TaskRunState) -> NodeResult:
    line = "review: ok"
    return NodeResult(data_update={"transcript": [*_transcript(state), line]})


def build_echo_graph() -> Graph:
    """Return a fresh, compiled instance of the echo graph."""

    g = Graph(ECHO_GRAPH_ID)
    g.add_node("plan", _plan)
    g.add_node("execute", _execute)
    g.add_node("review", _review)
    g.set_entry("plan")
    g.add_edge("plan", "execute")
    g.add_edge("execute", "review")
    g.add_edge("review", END)
    g.compile()
    return g
