"""Capability / Tool abstractions.

【目标】Tool 协议、注册表、错误模型、重试策略；MCP-ready。
【当前】Tool 端口（`meta_agent.core.ports.tools`）+ 注册表（`ToolRegistry`）
+ 执行器（`ToolExecutor`）三段 seam 已落地；
具体 FS / Edit 实现在 `meta_agent.infra.tools`。
"""

from meta_agent.core.capabilities.executor import ToolExecutor
from meta_agent.core.capabilities.registry import (
    RegisteredTool,
    ToolHandler,
    ToolRegistry,
)

__all__ = [
    "RegisteredTool",
    "ToolExecutor",
    "ToolHandler",
    "ToolRegistry",
]
