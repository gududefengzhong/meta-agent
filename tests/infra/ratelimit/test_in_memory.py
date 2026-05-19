"""Algorithmic unit tests for :class:`InMemoryTokenBucketRateLimiter`.

These tests pin the token-bucket semantics that the upcoming Redis
Lua adapter must replicate exactly. Time is injected so the algorithm
can be tested deterministically without ``asyncio.sleep``.
"""

from __future__ import annotations

import pytest

from meta_agent.infra.ratelimit.in_memory import InMemoryTokenBucketRateLimiter


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _build(rate: float, burst: int, clock: _FakeClock) -> InMemoryTokenBucketRateLimiter:
    return InMemoryTokenBucketRateLimiter(rate_per_sec=rate, burst=burst, monotonic=clock)


def test_construction_rejects_invalid_parameters() -> None:
    with pytest.raises(ValueError, match="rate_per_sec must be > 0"):
        InMemoryTokenBucketRateLimiter(rate_per_sec=0, burst=10)
    with pytest.raises(ValueError, match="burst must be >= 1"):
        InMemoryTokenBucketRateLimiter(rate_per_sec=1.0, burst=0)


async def test_new_bucket_starts_full() -> None:
    clock = _FakeClock()
    limiter = _build(rate=1.0, burst=5, clock=clock)
    decision = await limiter.acquire("k")
    assert decision.allowed is True
    assert decision.remaining == 4


async def test_burst_then_deny_then_refill() -> None:
    clock = _FakeClock()
    limiter = _build(rate=2.0, burst=3, clock=clock)
    # 3 quick calls drain the bucket without advancing time.
    for _ in range(3):
        assert (await limiter.acquire("k")).allowed is True
    # 4th call must be denied; bucket is empty.
    denied = await limiter.acquire("k")
    assert denied.allowed is False
    assert denied.remaining == 0
    # At 2 tokens/sec, 1 token refills in 500ms.
    assert denied.retry_after_ms is not None
    assert 400 <= denied.retry_after_ms <= 600
    # Wait long enough for 1 token, then 1 call must succeed.
    clock.advance(0.6)
    granted = await limiter.acquire("k")
    assert granted.allowed is True


async def test_refill_caps_at_burst() -> None:
    clock = _FakeClock()
    limiter = _build(rate=10.0, burst=2, clock=clock)
    # Drain
    await limiter.acquire("k")
    await limiter.acquire("k")
    # Wait long enough that refill would exceed burst without the cap.
    clock.advance(60.0)
    # Burst tokens recover; first 2 allowed, 3rd denied (no carry-over).
    assert (await limiter.acquire("k")).allowed is True
    assert (await limiter.acquire("k")).allowed is True
    assert (await limiter.acquire("k")).allowed is False


async def test_independent_keys_do_not_share_state() -> None:
    clock = _FakeClock()
    limiter = _build(rate=1.0, burst=1, clock=clock)
    assert (await limiter.acquire("tenant-a")).allowed is True
    # Same instant, different key: still allowed.
    assert (await limiter.acquire("tenant-b")).allowed is True
    # But repeating tenant-a without time advance is denied.
    assert (await limiter.acquire("tenant-a")).allowed is False


async def test_cost_greater_than_burst_always_denies() -> None:
    clock = _FakeClock()
    limiter = _build(rate=1.0, burst=5, clock=clock)
    decision = await limiter.acquire("k", cost=10)
    assert decision.allowed is False
    # Cannot retry into success — request itself exceeds capacity.
    assert decision.retry_after_ms is None
    # And the bucket is left intact.
    follow_up = await limiter.acquire("k", cost=5)
    assert follow_up.allowed is True


async def test_rejects_non_positive_cost() -> None:
    clock = _FakeClock()
    limiter = _build(rate=1.0, burst=5, clock=clock)
    with pytest.raises(ValueError, match="cost must be >= 1"):
        await limiter.acquire("k", cost=0)


async def test_denied_call_does_not_consume_tokens() -> None:
    clock = _FakeClock()
    limiter = _build(rate=1.0, burst=2, clock=clock)
    assert (await limiter.acquire("k", cost=2)).allowed is True
    # Bucket empty; the following cost=2 must deny but leave the bucket
    # untouched so a smaller cost can still be served after a refill.
    assert (await limiter.acquire("k", cost=2)).allowed is False
    clock.advance(1.0)  # +1 token
    # Bucket now has exactly 1 token — cost=1 succeeds, cost=2 denies.
    assert (await limiter.acquire("k", cost=1)).allowed is True
    assert (await limiter.acquire("k", cost=1)).allowed is False
