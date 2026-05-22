"""Orchestration layer.

【目标】LangGraph 风格的状态机：Plan → Act → Observe → Verify → Deliver；
含 checkpoint 外置、幂等键、人工确认中断点。
【当前】最小 runtime：``TaskRunState`` 不可变快照 + ``Graph`` 节点/边模型。
"""

from meta_agent.core.orchestration.chain import (
    TaskChainPolicy,
    TaskChainRegistry,
    bug_fix_to_auto_pr_policy,
)
from meta_agent.core.orchestration.deps import GraphDeps, GraphFactory
from meta_agent.core.orchestration.graph import (
    Graph,
    GraphError,
    NodeFn,
    NodeResult,
    RouterFn,
)
from meta_agent.core.orchestration.human_gate import (
    HUMAN_DECISION_KEY,
    HUMAN_FEEDBACK_KEY,
    HUMAN_GATE_AT_KEY,
    build_human_gate,
)
from meta_agent.core.orchestration.registry import GraphRegistry
from meta_agent.core.orchestration.result import (
    TaskError,
    TaskErrorCode,
    TaskResult,
    TaskResultStatus,
)
from meta_agent.core.orchestration.state import END, START, TaskRunState

__all__ = [
    "END",
    "HUMAN_DECISION_KEY",
    "HUMAN_FEEDBACK_KEY",
    "HUMAN_GATE_AT_KEY",
    "START",
    "Graph",
    "GraphDeps",
    "GraphError",
    "GraphFactory",
    "GraphRegistry",
    "NodeFn",
    "NodeResult",
    "RouterFn",
    "TaskChainPolicy",
    "TaskChainRegistry",
    "TaskError",
    "TaskErrorCode",
    "TaskResult",
    "TaskResultStatus",
    "TaskRunState",
    "bug_fix_to_auto_pr_policy",
    "build_human_gate",
]
