"""Unit tests for the ``builtin.shell_agent`` graph."""

from __future__ import annotations

from pathlib import Path

import pytest

from meta_agent.core.capabilities.registry import ToolRegistry
from meta_agent.core.orchestration import TaskRunState
from meta_agent.core.orchestration.graphs.shell_agent import (
    SHELL_AGENT_GRAPH_ID,
    build_shell_agent_graph,
)
from meta_agent.core.ports.tools import (
    ToolCall,
    ToolCategory,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from tests.core.orchestration._fakes import FakeLLMClient, fake_deps, make_response

pytestmark = pytest.mark.asyncio


def _state(**data: object) -> TaskRunState:
    return TaskRunState(
        task_id="task-1",
        tenant_id="tenant-1",
        trace_id="trace-1",
        graph_id=SHELL_AGENT_GRAPH_ID,
        data=data,
    )


def _spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="d",
        parameters={"type": "object"},
        category=ToolCategory.FILESYSTEM,
    )


def _registry_with(name: str, *, content: str = "tool-output") -> tuple[ToolRegistry, list[ToolCall]]:
    """Build a registry whose single tool records every call and returns ``content``."""
    registry = ToolRegistry()
    recorded: list[ToolCall] = []

    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        recorded.append(call)
        return ToolResult(call_id=call.id, name=call.name, content=content)

    registry.register(_spec(name), handler)
    return registry, recorded


async def test_loop_terminates_when_llm_returns_no_tool_calls() -> None:
    client = FakeLLMClient(response=make_response(content="final", model="fake/m1"))
    registry, _ = _registry_with("noop")
    deps = fake_deps(client, tool_registry=registry)
    graph = build_shell_agent_graph(deps)

    final = await graph.run(_state(user_prompt="hi"))

    assert final.finished is True
    output = final.data["output"]
    assert isinstance(output, dict)
    assert output["assistant_message"] == "final"
    assert output["steps"] == 1
    assert output["tool_invocations"] == 0
    assert output["truncated_by_max_steps"] is False
    assert len(client.calls) == 1


async def test_tool_call_executes_and_observation_feeds_next_plan(tmp_path: Path) -> None:
    registry, recorded = _registry_with("fs_read", content="hello")
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(ToolCall(id="c1", name="fs_read", arguments={"path": "x"}),),
                finish_reason="tool_call",
            ),
            make_response(content="all done", finish_reason="stop"),
        ]
    )
    deps = fake_deps(client, tool_registry=registry)
    graph = build_shell_agent_graph(deps)

    final = await graph.run(_state(user_prompt="hi", _workspace_path=str(tmp_path)))

    assert final.finished is True
    output = final.data["output"]
    assert output["assistant_message"] == "all done"  # type: ignore[index]
    assert output["steps"] == 2  # type: ignore[index]
    assert output["tool_invocations"] == 1  # type: ignore[index]
    assert len(recorded) == 1
    assert recorded[0].name == "fs_read"
    # second LLM call should have included the tool observation
    second_request = client.calls[1]
    roles = [m.role.value for m in second_request.messages]
    assert "tool" in roles


async def test_unknown_tool_surfaces_is_error_observation_then_completes() -> None:
    registry, _ = _registry_with("fs_read")
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(ToolCall(id="c1", name="nope", arguments={}),),
                finish_reason="tool_call",
            ),
            make_response(content="gave up", finish_reason="stop"),
        ]
    )
    deps = fake_deps(client, tool_registry=registry)
    graph = build_shell_agent_graph(deps)

    final = await graph.run(_state(user_prompt="hi"))

    assert final.finished is True
    # second LLM call's last tool message should be the is_error normalisation
    second_request = client.calls[1]
    tool_msg = second_request.messages[-1]
    assert tool_msg.role.value == "tool"
    assert "nope" in tool_msg.content


async def test_max_steps_cap_short_circuits_loop() -> None:
    registry, _ = _registry_with("fs_read")
    # LLM keeps demanding a tool call; after ``max_steps`` plan invocations the
    # graph must finalise even though the LLM is still requesting tools.
    client = FakeLLMClient(
        handler=lambda _req: make_response(
            content="",
            tool_calls=(ToolCall(id="c", name="fs_read", arguments={}),),
            finish_reason="tool_call",
        )
    )
    deps = fake_deps(client, tool_registry=registry)
    graph = build_shell_agent_graph(deps)

    final = await graph.run(_state(user_prompt="hi", max_steps=2))

    assert final.finished is True
    output = final.data["output"]
    assert output["steps"] == 2  # type: ignore[index]
    assert output["truncated_by_max_steps"] is True  # type: ignore[index]
    # plan was called exactly max_steps times
    assert len(client.calls) == 2
