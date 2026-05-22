"""Unit tests for the ``builtin.feature_impl`` graph.

feature_impl shares all node logic with ``builtin.shell_agent``; these
tests cover only the deltas:

* The graph identity propagates to ``Graph.graph_id``.
* When ``state.data['system_prompt']`` is missing, the registered
  ``feature_impl.system`` prompt is fetched and injected.
* A caller-supplied ``system_prompt`` still wins (no registry lookup).
* The outgoing :class:`LLMRequest` carries the resolved
  ``prompt_id`` + ``prompt_version`` for provenance.
* The worker bootstrap registers the graph as the default handler for
  ``TaskType.FEATURE_IMPL`` whenever tool capabilities are present.
"""

from __future__ import annotations

import pytest

from meta_agent.core.capabilities.registry import ToolRegistry
from meta_agent.core.domain.task import TaskType
from meta_agent.core.orchestration import TaskRunState
from meta_agent.core.orchestration.graph import GraphError
from meta_agent.core.orchestration.graphs.feature_impl import (
    FEATURE_IMPL_GRAPH_ID,
    FEATURE_IMPL_SYSTEM_PROMPT_ID,
    build_feature_impl_graph,
)
from meta_agent.core.ports.llm import MessageRole
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
        graph_id=FEATURE_IMPL_GRAPH_ID,
        data=data,
    )


def _empty_registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        return ToolResult(call_id=call.id, name=call.name, content="ok")

    registry.register(
        ToolSpec(
            name="fs_read",
            description="d",
            parameters={"type": "object"},
            category=ToolCategory.FILESYSTEM,
        ),
        handler,
    )
    return registry


async def test_graph_id_propagates_through_builder() -> None:
    deps = fake_deps(
        FakeLLMClient(response=make_response(content="done")),
        tool_registry=_empty_registry(),
    )
    graph = build_feature_impl_graph(deps)
    assert graph.graph_id == FEATURE_IMPL_GRAPH_ID
    assert graph.graph_id != "builtin.shell_agent"


async def test_default_system_prompt_resolved_from_registry() -> None:
    client = FakeLLMClient(response=make_response(content="final"))
    deps = fake_deps(client, tool_registry=_empty_registry())
    graph = build_feature_impl_graph(deps)

    await graph.run(_state(user_prompt="add a function"))

    assert len(client.calls) == 1
    request = client.calls[0]
    system_messages = [m for m in request.messages if m.role == MessageRole.SYSTEM]
    assert len(system_messages) == 1
    # The injected system message matches the registered seed verbatim.
    assert deps.prompt_registry is not None
    seed = await deps.prompt_registry.fetch(FEATURE_IMPL_SYSTEM_PROMPT_ID)
    assert system_messages[0].content == seed.content
    # Provenance flows to the LLMRequest.
    assert request.prompt_id == FEATURE_IMPL_SYSTEM_PROMPT_ID
    assert request.prompt_version == seed.version


async def test_caller_system_prompt_overrides_registry_and_drops_provenance() -> None:
    client = FakeLLMClient(response=make_response(content="final"))
    graph = build_feature_impl_graph(fake_deps(client, tool_registry=_empty_registry()))

    await graph.run(_state(user_prompt="hi", system_prompt="custom framing"))

    request = client.calls[0]
    system_messages = [m for m in request.messages if m.role == MessageRole.SYSTEM]
    assert len(system_messages) == 1
    assert system_messages[0].content == "custom framing"
    # Caller-owned text means no registered prompt drove the call.
    assert request.prompt_id is None
    assert request.prompt_version is None


async def test_user_prompt_still_required() -> None:
    graph = build_feature_impl_graph(
        fake_deps(FakeLLMClient(), tool_registry=_empty_registry())
    )

    with pytest.raises(GraphError, match="user_prompt"):
        await graph.run(_state())


async def test_bootstrap_resolves_feature_impl_to_this_graph() -> None:
    from meta_agent.worker.bootstrap import build_registry

    deps = fake_deps(FakeLLMClient(), tool_registry=_empty_registry())
    registry = build_registry(deps)

    assert registry.default_graph_id(TaskType.FEATURE_IMPL) == FEATURE_IMPL_GRAPH_ID
    graph = registry.resolve(TaskType.FEATURE_IMPL)
    assert graph.graph_id == FEATURE_IMPL_GRAPH_ID
    assert registry.requires_workspace(FEATURE_IMPL_GRAPH_ID) is True


async def test_bootstrap_skips_feature_impl_without_tool_capabilities() -> None:
    from meta_agent.worker.bootstrap import build_registry

    deps = fake_deps(FakeLLMClient())
    registry = build_registry(deps)

    assert registry.default_graph_id(TaskType.FEATURE_IMPL) is None
    with pytest.raises(GraphError, match="no default graph registered"):
        registry.resolve(TaskType.FEATURE_IMPL)
