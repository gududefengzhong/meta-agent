"""Unit tests for the local-workspace ``ToolHandler`` adapters."""

from __future__ import annotations

from pathlib import Path

import pytest

from meta_agent.core.capabilities.executor import ToolExecutor
from meta_agent.core.capabilities.registry import ToolRegistry
from meta_agent.core.ports.tools import (
    ToolCall,
    ToolContext,
    ToolValidationError,
)
from meta_agent.infra.tools.local_handlers import (
    TOOL_EDIT_PATCH_APPLY,
    TOOL_EDIT_WRITE,
    TOOL_FS_GREP,
    TOOL_FS_LIST_DIR,
    TOOL_FS_READ,
    register_local_workspace_tools,
)
from meta_agent.infra.tools.local_workspace import (
    LocalWorkspaceEditTool,
    LocalWorkspaceFileSystemTool,
)


def _ctx(workspace: Path) -> ToolContext:
    return ToolContext(
        tenant_id="t",
        task_id="task",
        trace_id="trace",
        workspace_path=workspace,
    )


@pytest.fixture
def populated_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_local_workspace_tools(
        registry,
        fs=LocalWorkspaceFileSystemTool(),
        edit=LocalWorkspaceEditTool(),
    )
    return registry


def test_register_local_workspace_tools_populates_all_five(
    populated_registry: ToolRegistry,
) -> None:
    assert populated_registry.names() == {
        TOOL_FS_READ,
        TOOL_FS_LIST_DIR,
        TOOL_FS_GREP,
        TOOL_EDIT_WRITE,
        TOOL_EDIT_PATCH_APPLY,
    }


def test_specs_sorted_by_name(populated_registry: ToolRegistry) -> None:
    names = [spec.name for spec in populated_registry.list_specs()]
    assert names == sorted(names)


def test_register_is_idempotent_only_via_fresh_registry() -> None:
    registry = ToolRegistry()
    register_local_workspace_tools(
        registry,
        fs=LocalWorkspaceFileSystemTool(),
        edit=LocalWorkspaceEditTool(),
    )
    with pytest.raises(ToolValidationError):
        register_local_workspace_tools(
            registry,
            fs=LocalWorkspaceFileSystemTool(),
            edit=LocalWorkspaceEditTool(),
        )


async def test_fs_read_handler_round_trip(
    populated_registry: ToolRegistry, tmp_path: Path
) -> None:
    (tmp_path / "x.txt").write_text("hello", encoding="utf-8")
    executor = ToolExecutor(populated_registry)
    call = ToolCall(id="c1", name=TOOL_FS_READ, arguments={"path": "x.txt"})
    result = await executor.execute(call, _ctx(tmp_path))
    assert result.is_error is False
    assert result.content == "hello"


async def test_fs_list_dir_handler_includes_subdirs(
    populated_registry: ToolRegistry, tmp_path: Path
) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "z.txt").write_text("z", encoding="utf-8")
    executor = ToolExecutor(populated_registry)
    call = ToolCall(id="c2", name=TOOL_FS_LIST_DIR, arguments={"path": ""})
    result = await executor.execute(call, _ctx(tmp_path))
    assert "a.txt" in result.content
    assert "z.txt" in result.content


async def test_fs_grep_handler_returns_formatted_hits(
    populated_registry: ToolRegistry, tmp_path: Path
) -> None:
    (tmp_path / "x.txt").write_text("aaa\nbbb\n", encoding="utf-8")
    executor = ToolExecutor(populated_registry)
    call = ToolCall(
        id="c3",
        name=TOOL_FS_GREP,
        arguments={"pattern": "aaa", "path_globs": ["**/*.txt"]},
    )
    result = await executor.execute(call, _ctx(tmp_path))
    assert result.is_error is False
    assert result.content.startswith("x.txt:1:aaa")


async def test_edit_write_handler_writes_file(
    populated_registry: ToolRegistry, tmp_path: Path
) -> None:
    executor = ToolExecutor(populated_registry)
    call = ToolCall(
        id="c4",
        name=TOOL_EDIT_WRITE,
        arguments={"path": "out.txt", "content": "hi"},
    )
    result = await executor.execute(call, _ctx(tmp_path))
    assert result.is_error is False
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "hi"
    assert result.metadata == {"bytes_written": "2"}


async def test_handler_missing_required_argument_surfaces_validation_error(
    populated_registry: ToolRegistry, tmp_path: Path
) -> None:
    executor = ToolExecutor(populated_registry)
    call = ToolCall(id="c5", name=TOOL_FS_READ, arguments={})
    result = await executor.execute(call, _ctx(tmp_path))
    assert result.is_error is True
    assert "path" in result.content


async def test_handler_wrong_type_argument_surfaces_validation_error(
    populated_registry: ToolRegistry, tmp_path: Path
) -> None:
    executor = ToolExecutor(populated_registry)
    call = ToolCall(id="c6", name=TOOL_FS_READ, arguments={"path": 42})
    result = await executor.execute(call, _ctx(tmp_path))
    assert result.is_error is True


async def test_unknown_tool_name_via_executor(
    populated_registry: ToolRegistry, tmp_path: Path
) -> None:
    executor = ToolExecutor(populated_registry)
    call = ToolCall(id="c7", name="nope", arguments={})
    result = await executor.execute(call, _ctx(tmp_path))
    assert result.is_error is True
    assert "nope" in result.content
