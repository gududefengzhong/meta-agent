"""Rate-limiter adapters.

【目标】Redis 令牌桶（跨副本共享）+ in-memory（单进程 / 单测）+
NoOp（默认零拦截），通过 env 切换。

【当前】NoOp + in-memory + Redis Lua + env-driven factory。
"""

from meta_agent.infra.ratelimit.config import (
    Backend,
    RateLimitConfig,
    build_rate_limiter_from_config,
)
from meta_agent.infra.ratelimit.in_memory import InMemoryTokenBucketRateLimiter
from meta_agent.infra.ratelimit.noop import NoopRateLimiter
from meta_agent.infra.ratelimit.redis_token_bucket import RedisTokenBucketRateLimiter

__all__ = [
    "Backend",
    "InMemoryTokenBucketRateLimiter",
    "NoopRateLimiter",
    "RateLimitConfig",
    "RedisTokenBucketRateLimiter",
    "build_rate_limiter_from_config",
]
