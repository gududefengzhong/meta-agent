"""shell_agent end-to-end smoke covering the Phase β+ WEB tool surface.

Exercises the full loop without DB / Redis / workspace: ``shell_agent``
runs against a :class:`ToolRegistry` populated with ``web_fetch`` and
``doc_search`` handlers; the LLM is scripted to call each tool once,
then finalise. Asserts that the rendered tool observations carry the
expected status / hit data so the model has enough signal to continue.
"""

from __future__ import annotations

import httpx

from meta_agent.core.capabilities.executor import ToolExecutor
from meta_agent.core.capabilities.registry import ToolRegistry
from meta_agent.core.orchestration import TaskRunState
from meta_agent.core.orchestration.graphs.shell_agent import (
    SHELL_AGENT_GRAPH_ID,
    build_shell_agent_graph,
)
from meta_agent.core.ports.tools import ToolCall
from meta_agent.infra.tools.doc_search import DocEntry, InMemoryDocSearchTool
from meta_agent.infra.tools.local_handlers import (
    TOOL_DOC_SEARCH,
    TOOL_WEB_FETCH,
    register_local_workspace_tools,
)
from meta_agent.infra.tools.local_workspace import (
    LocalWorkspaceEditTool,
    LocalWorkspaceFileSystemTool,
    LocalWorkspaceShellTool,
    LocalWorkspaceTestTool,
)
from meta_agent.infra.tools.web_fetch import HttpxWebFetchTool
from tests.core.orchestration._fakes import FakeLLMClient, fake_deps, make_response


def _state(**data: object) -> TaskRunState:
    return TaskRunState(
        task_id="task-1",
        tenant_id="tenant-1",
        trace_id="trace-1",
        graph_id=SHELL_AGENT_GRAPH_ID,
        data=data,
    )


def _registry_with_web_and_doc() -> tuple[ToolRegistry, ToolExecutor]:
    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"Postgres 16 is the current production version.",
            headers={"content-type": "text/plain; charset=utf-8"},
        )

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(transport))

    web = HttpxWebFetchTool(
        allowed_hosts=frozenset({"docs.example.com"}),
        client_factory=factory,
    )
    doc = InMemoryDocSearchTool(
        (
            DocEntry(
                source_uri="doc://infra/postgres",
                title="Postgres deployment runbook",
                body=(
                    "Use psql to connect; the cluster runs Postgres 16. "
                    "See the failover checklist for replica rotation."
                ),
            ),
            DocEntry(
                source_uri="doc://infra/redis",
                title="Redis runbook",
                body="Use redis-cli ping to confirm the primary.",
            ),
        )
    )
    registry = ToolRegistry()
    register_local_workspace_tools(
        registry,
        fs=LocalWorkspaceFileSystemTool(),
        edit=LocalWorkspaceEditTool(),
        shell=LocalWorkspaceShellTool(allowed_commands=frozenset({"python"})),
        test=LocalWorkspaceTestTool(),
        web_fetch=web,
        doc_search=doc,
    )
    return registry, ToolExecutor(registry)


async def test_shell_agent_loops_through_doc_search_then_web_fetch_then_finalises() -> None:
    registry, executor = _registry_with_web_and_doc()
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name=TOOL_DOC_SEARCH,
                        arguments={"query": "Postgres version", "limit": 1},
                    ),
                ),
                finish_reason="tool_call",
            ),
            make_response(
                content="",
                tool_calls=(
                    ToolCall(
                        id="c2",
                        name=TOOL_WEB_FETCH,
                        arguments={"url": "https://docs.example.com/postgres"},
                    ),
                ),
                finish_reason="tool_call",
            ),
            make_response(content="found Postgres 16", finish_reason="stop"),
        ]
    )
    deps = fake_deps(client, tool_registry=registry, tool_executor=executor)
    graph = build_shell_agent_graph(deps)

    final = await graph.run(_state(user_prompt="What Postgres version do we run?"))

    output = final.data["output"]
    assert isinstance(output, dict)
    assert output["assistant_message"] == "found Postgres 16"
    assert output["steps"] == 3
    assert output["tool_invocations"] == 2

    # Tool observations should carry the expected substrings so the LLM
    # has useful context on the next turn.
    second_request = client.calls[1]
    doc_observation = second_request.messages[-1]
    assert doc_observation.role.value == "tool"
    assert "doc://infra/postgres" in doc_observation.content
    assert "Postgres" in doc_observation.content

    third_request = client.calls[2]
    web_observation = third_request.messages[-1]
    assert web_observation.role.value == "tool"
    assert "status=200" in web_observation.content
    assert "Postgres 16" in web_observation.content


async def test_shell_agent_surfaces_web_fetch_permission_error_as_tool_observation() -> None:
    # Domain allow-list refuses the URL — the executor renders this
    # as ``is_error=True`` so the agent can branch on it.
    def transport(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("web_fetch should not have fired")

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(transport))

    web = HttpxWebFetchTool(
        allowed_hosts=frozenset({"docs.example.com"}),
        client_factory=factory,
    )
    registry = ToolRegistry()
    register_local_workspace_tools(
        registry,
        fs=LocalWorkspaceFileSystemTool(),
        edit=LocalWorkspaceEditTool(),
        shell=LocalWorkspaceShellTool(allowed_commands=frozenset({"python"})),
        test=LocalWorkspaceTestTool(),
        web_fetch=web,
    )
    executor = ToolExecutor(registry)
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name=TOOL_WEB_FETCH,
                        arguments={"url": "https://other.example.org/forbidden"},
                    ),
                ),
                finish_reason="tool_call",
            ),
            make_response(content="gave up after permission error", finish_reason="stop"),
        ]
    )
    deps = fake_deps(client, tool_registry=registry, tool_executor=executor)
    graph = build_shell_agent_graph(deps)

    final = await graph.run(_state(user_prompt="fetch anything"))
    assert final.finished is True
    second = client.calls[1]
    obs = second.messages[-1]
    assert obs.role.value == "tool"
    assert "tool_status=error" in obs.content
    assert "not in allow-list" in obs.content
