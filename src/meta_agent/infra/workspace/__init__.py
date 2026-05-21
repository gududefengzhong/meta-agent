"""Workspace adapters.

【当前】LocalGitWorkspaceManager: 单机 ``git worktree`` 实现，每个任务
一个隔离工作树 + 专属 feature 分支。
【Phase β 当前切片】DockerWorkspaceManager: 复用本地 ``git worktree``
provisioning，并给每个 workspace 配一个 companion container；工具执行
尚未全部切到容器内。
【目标】真正容器化沙盒、共享 bare 仓库缓存、远端 build server 等替代实现。
"""

from meta_agent.infra.workspace.docker_workspace import (
    DockerWorkspaceConfig,
    DockerWorkspaceManager,
)
from meta_agent.infra.workspace.local_git import (
    LocalGitConfig,
    LocalGitWorkspaceManager,
)

__all__ = [
    "DockerWorkspaceConfig",
    "DockerWorkspaceManager",
    "LocalGitConfig",
    "LocalGitWorkspaceManager",
]
