"""Orchestration layer.

【目标】LangGraph 风格的状态机：Plan → Act → Observe → Verify → Deliver；
含 checkpoint 外置、幂等键、人工确认中断点。
【当前】最小 runtime：``TaskRunState`` 不可变快照 + ``Graph`` 节点/边模型。
"""

from meta_agent.core.orchestration.graph import (
    Graph,
    GraphError,
    NodeFn,
    NodeResult,
    RouterFn,
)
from meta_agent.core.orchestration.registry import GraphRegistry
from meta_agent.core.orchestration.state import END, START, TaskRunState

__all__ = [
    "END",
    "START",
    "Graph",
    "GraphError",
    "GraphRegistry",
    "NodeFn",
    "NodeResult",
    "RouterFn",
    "TaskRunState",
]
