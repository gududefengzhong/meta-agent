"""Circuit-breaker adapters.

【目标】Redis 共享计数（跨副本）+ in-memory（单进程 / 单测）+ NoOp
（默认零拦截），通过 env 切换；接 LLM provider / Git provider / Tool。

【当前】NoOp + in-memory（三态 + 滑窗失败计数）+ Redis 共享版。
通过 ``META_AGENT_CIRCUITBREAKER_BACKEND`` 选择后端。
"""

from meta_agent.infra.circuitbreaker.config import (
    Backend,
    CircuitBreakerConfig,
    build_circuit_breaker_from_config,
)
from meta_agent.infra.circuitbreaker.in_memory import InMemoryCircuitBreaker
from meta_agent.infra.circuitbreaker.noop import NoopCircuitBreaker
from meta_agent.infra.circuitbreaker.redis_breaker import RedisCircuitBreaker

__all__ = [
    "Backend",
    "CircuitBreakerConfig",
    "InMemoryCircuitBreaker",
    "NoopCircuitBreaker",
    "RedisCircuitBreaker",
    "build_circuit_breaker_from_config",
]
