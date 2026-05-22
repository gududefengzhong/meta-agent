"""shell_agent end-to-end smoke covering the Phase β+ CODE_INDEX tool surface.

Exercises the full plan→tool→observe loop against a fixture Python
repo: the scripted LLM walks ``outline → get_definition →
code_search`` to mimic how a real agent would navigate. Asserts each
tool observation carries the expected substrings so downstream LLM
turns have useful context.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meta_agent.core.capabilities.executor import ToolExecutor
from meta_agent.core.capabilities.registry import ToolRegistry
from meta_agent.core.orchestration import TaskRunState
from meta_agent.core.orchestration.graphs.shell_agent import (
    SHELL_AGENT_GRAPH_ID,
    build_shell_agent_graph,
)
from meta_agent.core.ports.tools import ToolCall
from meta_agent.infra.tools.code_index import TreeSitterCodeRetrievalTool
from meta_agent.infra.tools.local_handlers import (
    TOOL_CODE_SEARCH,
    TOOL_GET_DEFINITION,
    TOOL_OUTLINE,
    register_local_workspace_tools,
)
from meta_agent.infra.tools.local_workspace import (
    LocalWorkspaceEditTool,
    LocalWorkspaceFileSystemTool,
    LocalWorkspaceShellTool,
    LocalWorkspaceTestTool,
)
from tests.core.orchestration._fakes import FakeLLMClient, fake_deps, make_response

_FIXTURE = """\
class Calculator:
    def add(self, a, b):
        return a + b

    def mul(self, a, b):
        return a * b


def helper():
    return Calculator()
"""


def _state(workspace: Path) -> TaskRunState:
    return TaskRunState(
        task_id="task-1",
        tenant_id="tenant-1",
        trace_id="trace-1",
        graph_id=SHELL_AGENT_GRAPH_ID,
        data={
            "user_prompt": "find the Calculator.mul implementation",
            "_workspace_path": str(workspace),
        },
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(_FIXTURE, encoding="utf-8")
    return tmp_path


def _registry_with_code_index(workspace: Path) -> tuple[ToolRegistry, ToolExecutor]:
    registry = ToolRegistry()
    register_local_workspace_tools(
        registry,
        fs=LocalWorkspaceFileSystemTool(),
        edit=LocalWorkspaceEditTool(),
        shell=LocalWorkspaceShellTool(allowed_commands=frozenset({"python"})),
        test=LocalWorkspaceTestTool(),
        code_retrieval=TreeSitterCodeRetrievalTool(),
    )
    return registry, ToolExecutor(registry)


async def test_shell_agent_navigates_outline_then_definition_then_search(
    workspace: Path,
) -> None:
    registry, executor = _registry_with_code_index(workspace)
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name=TOOL_OUTLINE,
                        arguments={"path": "src/calc.py"},
                    ),
                ),
                finish_reason="tool_call",
            ),
            make_response(
                content="",
                tool_calls=(
                    ToolCall(
                        id="c2",
                        name=TOOL_GET_DEFINITION,
                        arguments={"symbol": "mul"},
                    ),
                ),
                finish_reason="tool_call",
            ),
            make_response(
                content="",
                tool_calls=(
                    ToolCall(
                        id="c3",
                        name=TOOL_CODE_SEARCH,
                        arguments={"query": "return a"},
                    ),
                ),
                finish_reason="tool_call",
            ),
            make_response(content="found Calculator.mul", finish_reason="stop"),
        ]
    )
    deps = fake_deps(client, tool_registry=registry, tool_executor=executor)
    graph = build_shell_agent_graph(deps)

    final = await graph.run(_state(workspace))

    output = final.data["output"]
    assert isinstance(output, dict)
    assert output["assistant_message"] == "found Calculator.mul"
    assert output["steps"] == 4
    assert output["tool_invocations"] == 3

    # Outline observation should expose the class + methods.
    outline_obs = client.calls[1].messages[-1]
    assert outline_obs.role.value == "tool"
    assert "Calculator" in outline_obs.content
    assert "method mul" in outline_obs.content

    # get_definition observation pins ``mul`` to a precise file:line.
    def_obs = client.calls[2].messages[-1]
    assert "src/calc.py" in def_obs.content
    assert "method" in def_obs.content
    assert "mul" in def_obs.content

    # code_search observation should enrich the hit with the enclosing
    # symbol (``mul`` for the ``return a * b`` line).
    search_obs = client.calls[3].messages[-1]
    assert "src/calc.py" in search_obs.content
    assert "return a" in search_obs.content


async def test_outline_handler_renders_no_symbols_when_file_is_empty(
    workspace: Path,
) -> None:
    (workspace / "src" / "empty.py").write_text("", encoding="utf-8")
    _registry, executor = _registry_with_code_index(workspace)
    from meta_agent.core.ports.tools import ToolContext

    result = await executor.execute(
        ToolCall(id="c", name=TOOL_OUTLINE, arguments={"path": "src/empty.py"}),
        ToolContext(
            tenant_id="t",
            task_id="x",
            trace_id="y",
            workspace_path=workspace,
            output_byte_cap=64_000,
        ),
    )
    assert result.metadata == {"entries": "0"}
    assert "no symbols" in result.content
