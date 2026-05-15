"""Composition smoke test for the orchestration registry.

This test exercises the full bootstrap path that production code will
use at worker start-up: register the built-in graph factories, wire a
:class:`GraphDeps` container with the fake LLM, materialize the
registry, resolve the chat graph by task type and drive it to
completion. It is deliberately thin — adapter-level behaviour and
worker plumbing are covered by their own test suites.
"""

from __future__ import annotations

import pytest

from meta_agent.core.domain.task import TaskType
from meta_agent.core.orchestration import GraphRegistry, TaskRunState
from meta_agent.core.orchestration.graphs import (
    ECHO_GRAPH_ID,
    SIMPLE_CHAT_GRAPH_ID,
    build_echo_graph,
    build_simple_chat_graph,
)
from tests.core.orchestration._fakes import FakeLLMClient, fake_deps, make_response

pytestmark = pytest.mark.asyncio


async def test_bootstrap_registers_materializes_and_runs_simple_chat() -> None:
    client = FakeLLMClient(response=make_response(content="hi from llm", model="fake/m1"))
    deps = fake_deps(client)

    registry = GraphRegistry()
    registry.register(
        ECHO_GRAPH_ID,
        lambda _deps: build_echo_graph(),
        default_for=TaskType.SYSTEM_ECHO,
    )
    registry.register(
        SIMPLE_CHAT_GRAPH_ID,
        build_simple_chat_graph,
        default_for=TaskType.SYSTEM_CHAT,
    )
    registry.materialize(deps)

    # Routing: SYSTEM_ECHO -> echo, SYSTEM_CHAT -> simple_chat.
    assert registry.resolve(TaskType.SYSTEM_ECHO).graph_id == ECHO_GRAPH_ID
    chat_graph = registry.resolve(TaskType.SYSTEM_CHAT)
    assert chat_graph.graph_id == SIMPLE_CHAT_GRAPH_ID

    # Drive simple_chat through resolve() (mirrors what the worker does).
    final = await chat_graph.run(
        TaskRunState(
            task_id="task-1",
            tenant_id="tenant-1",
            trace_id="trace-1",
            graph_id=SIMPLE_CHAT_GRAPH_ID,
            data={"user_prompt": "hello"},
        )
    )

    assert final.finished is True
    assert final.data["assistant_message"] == "hi from llm"
    assert final.data["model_used"] == "fake/m1"
    assert len(client.calls) == 1


async def test_bootstrap_routes_explicit_graph_id_override() -> None:
    deps = fake_deps()
    registry = GraphRegistry()
    registry.register(
        ECHO_GRAPH_ID,
        lambda _deps: build_echo_graph(),
        default_for=TaskType.SYSTEM_ECHO,
    )
    registry.register(SIMPLE_CHAT_GRAPH_ID, build_simple_chat_graph)
    registry.materialize(deps)

    # SYSTEM_ECHO defaults to echo; explicit graph_id pins simple_chat.
    resolved = registry.resolve(TaskType.SYSTEM_ECHO, graph_id=SIMPLE_CHAT_GRAPH_ID)
    assert resolved.graph_id == SIMPLE_CHAT_GRAPH_ID
