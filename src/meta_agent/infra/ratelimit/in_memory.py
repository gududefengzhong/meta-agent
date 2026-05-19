"""In-memory token-bucket :class:`RateLimiter`.

Single-process implementation: state lives in a dict guarded by an
``asyncio.Lock``. Useful as the dev / unit-test default and as the
algorithmic reference for the upcoming Redis Lua adapter — both must
agree on bucket semantics so swapping adapters is observably a no-op.

Token-bucket semantics
======================
* Each ``key`` owns one bucket with capacity ``burst`` and a refill
  rate of ``rate_per_sec`` tokens / second.
* ``acquire(key, cost=c)``:
    - Refill: ``tokens = min(burst, tokens + (now - last_refill) * rate)``
    - If ``tokens >= c``: ``tokens -= c``, return ``allowed=True``.
    - Else: leave bucket untouched, return ``allowed=False`` with
      ``retry_after_ms = ceil((c - tokens) / rate * 1000)``.
* ``cost > burst`` always denies; the bucket cannot grow above burst.

Concurrency
-----------
A single ``asyncio.Lock`` per limiter instance serialises mutations.
This is fine for the in-process use case; the Redis adapter relies on
Lua atomicity instead.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass

from meta_agent.core.ports.rate_limiter import RateLimitDecision, RateLimiter


@dataclass(slots=True)
class _Bucket:
    tokens: float
    last_refill: float


class InMemoryTokenBucketRateLimiter(RateLimiter):
    """Single-process token bucket keyed by opaque string.

    Parameters
    ----------
    rate_per_sec:
        Refill rate in tokens / second. Must be ``> 0``.
    burst:
        Bucket capacity (maximum tokens). Must be ``>= 1``. New buckets
        start full so the first call never waits.
    monotonic:
        Override for the monotonic clock; injected by tests.
    """

    def __init__(
        self,
        *,
        rate_per_sec: float,
        burst: int,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be > 0")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self._rate = float(rate_per_sec)
        self._burst = int(burst)
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._lock = asyncio.Lock()
        self._buckets: dict[str, _Bucket] = {}

    async def acquire(self, key: str, *, cost: int = 1) -> RateLimitDecision:
        if cost < 1:
            raise ValueError("cost must be >= 1")
        if cost > self._burst:
            # A single call that exceeds burst can never succeed; deny
            # immediately with no retry hint (the caller cannot wait
            # this out — the request itself is too large).
            return RateLimitDecision(allowed=False, remaining=0, retry_after_ms=None)

        async with self._lock:
            now = self._monotonic()
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(self._burst), last_refill=now)
                self._buckets[key] = bucket
            else:
                elapsed = max(0.0, now - bucket.last_refill)
                bucket.tokens = min(float(self._burst), bucket.tokens + elapsed * self._rate)
                bucket.last_refill = now

            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return RateLimitDecision(
                    allowed=True,
                    remaining=int(bucket.tokens),
                    retry_after_ms=None,
                )
            deficit = cost - bucket.tokens
            retry_ms = max(1, math.ceil(deficit / self._rate * 1000))
            return RateLimitDecision(
                allowed=False,
                remaining=int(bucket.tokens),
                retry_after_ms=retry_ms,
            )


__all__ = ["InMemoryTokenBucketRateLimiter"]
