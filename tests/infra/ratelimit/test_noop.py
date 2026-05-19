"""Unit tests for :class:`NoopRateLimiter`."""

from __future__ import annotations

import pytest

from meta_agent.infra.ratelimit.noop import NoopRateLimiter


async def test_always_allows_with_advisory_remaining() -> None:
    limiter = NoopRateLimiter()
    decision = await limiter.acquire("any-key")
    assert decision.allowed is True
    assert decision.remaining > 0
    assert decision.retry_after_ms is None


async def test_allows_arbitrary_positive_cost() -> None:
    limiter = NoopRateLimiter()
    decision = await limiter.acquire("k", cost=10_000)
    assert decision.allowed is True


async def test_rejects_non_positive_cost() -> None:
    limiter = NoopRateLimiter()
    with pytest.raises(ValueError, match="cost must be >= 1"):
        await limiter.acquire("k", cost=0)
