"""Local adapters for the Phase β tool surface.

【目标】FileSystem / Edit / Shell / Test tool 的本地与容器化实现。
【当前】FileSystem / Edit / Shell / Test 的本地与容器化实现，并通过
``register_local_workspace_tools`` 注入 ``ToolRegistry``。

Concrete adapters live next to one another (one file per backend),
not per-tool, so a future Docker / Firecracker backend can be added
as a sibling module without restructuring the import graph.
"""

from meta_agent.infra.tools.doc_search import DocEntry, InMemoryDocSearchTool
from meta_agent.infra.tools.docker_workspace import (
    DockerWorkspaceEditTool,
    DockerWorkspaceFileSystemTool,
    DockerWorkspaceShellTool,
    DockerWorkspaceTestTool,
)
from meta_agent.infra.tools.local_handlers import (
    TOOL_DOC_SEARCH,
    TOOL_EDIT_PATCH_APPLY,
    TOOL_EDIT_WRITE,
    TOOL_FS_GREP,
    TOOL_FS_LIST_DIR,
    TOOL_FS_READ,
    TOOL_SHELL_RUN,
    TOOL_TEST_RUN,
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

__all__ = [
    "TOOL_DOC_SEARCH",
    "TOOL_EDIT_PATCH_APPLY",
    "TOOL_EDIT_WRITE",
    "TOOL_FS_GREP",
    "TOOL_FS_LIST_DIR",
    "TOOL_FS_READ",
    "TOOL_SHELL_RUN",
    "TOOL_TEST_RUN",
    "TOOL_WEB_FETCH",
    "DocEntry",
    "DockerWorkspaceEditTool",
    "DockerWorkspaceFileSystemTool",
    "DockerWorkspaceShellTool",
    "DockerWorkspaceTestTool",
    "HttpxWebFetchTool",
    "InMemoryDocSearchTool",
    "LocalWorkspaceEditTool",
    "LocalWorkspaceFileSystemTool",
    "LocalWorkspaceShellTool",
    "LocalWorkspaceTestTool",
    "register_local_workspace_tools",
]
