"""Workspace adapters.

【当前】LocalGitWorkspaceManager: 单机 ``git worktree`` 实现，每个任务
一个隔离工作树 + 专属 feature 分支。
【目标】容器化沙盒、共享 bare 仓库缓存、远端 build server 等替代实现。
"""

from meta_agent.infra.workspace.local_git import (
    LocalGitConfig,
    LocalGitWorkspaceManager,
)

__all__ = [
    "LocalGitConfig",
    "LocalGitWorkspaceManager",
]
