"""Redis-backed token-bucket :class:`RateLimiter`.

The bucket state is stored in a Redis ``HASH`` per key:

    {tokens: float, last_ms: int}

All read-modify-write is done in a single ``EVAL`` so concurrent
workers cannot race. The Lua script mirrors :class:`InMemoryTokenBucketRateLimiter`
exactly so swapping backends is observably a no-op:

* New bucket starts full.
* ``tokens = min(burst, tokens + (elapsed_ms / 1000.0) * rate)``.
* ``cost > burst`` always denies with ``retry_after_ms = None``.
* Denied calls leave ``tokens`` untouched but **do** advance the
  refill timestamp (matches the in-memory implementation).

Time source
-----------
The current millisecond timestamp is computed **client-side** and
passed in as ``ARGV``. This keeps the limiter independent of Redis'
server clock and lets unit tests inject a fake clock. The integration
test uses :func:`time.monotonic` so refill behaviour is observable.

Errors
------
Any :class:`RedisError` or Lua failure is wrapped in
:class:`RateLimiterBackendError` so the wrapper's fail-open / fail-closed
policy can act on it without parsing message strings.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from meta_agent.core.ports.rate_limiter import (
    RateLimitDecision,
    RateLimiter,
    RateLimiterBackendError,
)

# Lua script: refill, decide, write back atomically.
# Returns a 3-tuple ``{allowed, remaining, retry_ms}`` where
# ``retry_ms == -1`` is the sentinel for "request itself exceeds
# capacity, retry is impossible" (mapped to ``None`` on the Python side).
_LUA_SCRIPT: Final[str] = """
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local now_ms = tonumber(ARGV[4])

if cost > burst then
  return {0, 0, -1}
end

local data = redis.call('HMGET', KEYS[1], 'tokens', 'last_ms')
local tokens
local last_ms

if data[1] == false then
  tokens = burst
  last_ms = now_ms
else
  tokens = tonumber(data[1])
  last_ms = tonumber(data[2])
  local elapsed_ms = math.max(0, now_ms - last_ms)
  tokens = math.min(burst, tokens + (elapsed_ms / 1000.0) * rate)
  last_ms = now_ms
end

local allowed
local retry_ms
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
  retry_ms = 0
else
  local deficit = cost - tokens
  retry_ms = math.ceil(deficit / rate * 1000)
  if retry_ms < 1 then
    retry_ms = 1
  end
  allowed = 0
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'last_ms', last_ms)
local ttl_sec = math.ceil(burst / rate * 2)
if ttl_sec < 1 then
  ttl_sec = 1
end
redis.call('EXPIRE', KEYS[1], ttl_sec)

return {allowed, math.floor(tokens), retry_ms}
"""


class RedisTokenBucketRateLimiter(RateLimiter):
    """Redis-backed cooperative token bucket keyed by opaque string.

    The Redis client lifecycle is owned by the caller (the same client
    pool feeds the message-queue adapters); :meth:`close` is a no-op
    so the limiter does not accidentally tear down a shared connection.
    """

    def __init__(
        self,
        client: Redis,
        *,
        rate_per_sec: float,
        burst: int,
        key_prefix: str = "",
        monotonic_ms: Callable[[], int] | None = None,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self._client = client
        self._rate = float(rate_per_sec)
        self._burst = int(burst)
        self._prefix = key_prefix
        self._monotonic_ms = monotonic_ms if monotonic_ms is not None else _default_now_ms
        self._script = client.register_script(_LUA_SCRIPT)

    async def acquire(self, key: str, *, cost: int = 1) -> RateLimitDecision:
        if cost < 1:
            raise ValueError("cost must be >= 1")
        redis_key = f"{self._prefix}{key}" if self._prefix else key
        now_ms = self._monotonic_ms()
        try:
            raw = await self._script(
                keys=[redis_key],
                args=[self._rate, self._burst, cost, now_ms],
            )
        except RedisError as exc:
            raise RateLimiterBackendError(f"redis EVAL failed: {exc}") from exc

        allowed_i, remaining_i, retry_ms_i = (int(x) for x in raw)
        return RateLimitDecision(
            allowed=bool(allowed_i),
            remaining=int(remaining_i),
            retry_after_ms=None if retry_ms_i <= 0 else int(retry_ms_i),
        )


def _default_now_ms() -> int:
    return math.floor(time.monotonic() * 1000)


__all__ = ["RedisTokenBucketRateLimiter"]
