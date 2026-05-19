"""Rate-limiter adapters.

【目标】Redis 令牌桶（跨副本共享）+ in-memory（单进程 / 单测）+
NoOp（默认零拦截）。

【当前】NoOp + in-memory 令牌桶。Redis 实现留下个 PR。
"""

from meta_agent.infra.ratelimit.in_memory import InMemoryTokenBucketRateLimiter
from meta_agent.infra.ratelimit.noop import NoopRateLimiter

__all__ = [
    "InMemoryTokenBucketRateLimiter",
    "NoopRateLimiter",
]
