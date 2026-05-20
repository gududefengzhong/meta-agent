"""Circuit-breaker adapters.

【目标】Redis 共享计数（跨副本）+ in-memory（单进程 / 单测）+ NoOp
（默认零拦截），通过 env 切换；接 LLM provider / Git provider / Tool。

【当前】NoOp + in-memory（三态 + 滑窗失败计数）。Redis 共享版与 env
工厂留下个 PR。
"""

from meta_agent.infra.circuitbreaker.in_memory import InMemoryCircuitBreaker
from meta_agent.infra.circuitbreaker.noop import NoopCircuitBreaker

__all__ = [
    "InMemoryCircuitBreaker",
    "NoopCircuitBreaker",
]
