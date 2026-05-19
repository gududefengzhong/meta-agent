"""End-to-end :class:`RedisTokenBucketRateLimiter` against a real Redis.

The unit test in ``tests/infra/ratelimit/test_redis_token_bucket.py``
exercises the *wrapper* logic with a faked Lua script. This module
goes one level deeper: the actual Lua refill-and-decide path is run
inside Redis so any drift between the documented contract and the
script's behaviour is caught immediately.

Each test injects a deterministic monotonic clock so refill windows
are exact and the suite stays fast (no real ``asyncio.sleep`` between
phases).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from redis.asyncio import Redis

from meta_agent.infra.ratelimit.redis_token_bucket import RedisTokenBucketRateLimiter


class _FakeClock:
    """Monotonic-ms source whose value the test advances explicitly."""

    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self.now_ms = start_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance_ms(self, delta: int) -> None:
        self.now_ms += delta


def _key_prefix(request: pytest.FixtureRequest) -> str:
    # Namespace per test so no manual cleanup needed across runs.
    return f"rl:test:{request.node.name}:"


@pytest.fixture
def clock() -> Iterator[_FakeClock]:
    yield _FakeClock()


async def test_new_bucket_starts_full(
    redis_client: Redis, clock: _FakeClock, request: pytest.FixtureRequest
) -> None:
    limiter = RedisTokenBucketRateLimiter(
        redis_client,
        rate_per_sec=1.0,
        burst=5,
        key_prefix=_key_prefix(request),
        monotonic_ms=clock,
    )
    decision = await limiter.acquire("k")
    assert decision.allowed is True
    assert decision.remaining == 4


async def test_burst_then_deny_then_refill(
    redis_client: Redis, clock: _FakeClock, request: pytest.FixtureRequest
) -> None:
    limiter = RedisTokenBucketRateLimiter(
        redis_client,
        rate_per_sec=2.0,
        burst=3,
        key_prefix=_key_prefix(request),
        monotonic_ms=clock,
    )
    # 3 quick acquires drain the bucket.
    for _ in range(3):
        assert (await limiter.acquire("k")).allowed is True
    denied = await limiter.acquire("k")
    assert denied.allowed is False
    # 2 tokens/sec → 1 token refills in 500ms.
    assert denied.retry_after_ms is not None
    assert 400 <= denied.retry_after_ms <= 600
    # Advance the clock; bucket should now refill exactly 1 token.
    clock.advance_ms(600)
    granted = await limiter.acquire("k")
    assert granted.allowed is True


async def test_refill_caps_at_burst(
    redis_client: Redis, clock: _FakeClock, request: pytest.FixtureRequest
) -> None:
    limiter = RedisTokenBucketRateLimiter(
        redis_client,
        rate_per_sec=10.0,
        burst=2,
        key_prefix=_key_prefix(request),
        monotonic_ms=clock,
    )
    # Drain.
    await limiter.acquire("k")
    await limiter.acquire("k")
    # Wait long enough that an uncapped refill would overflow burst.
    clock.advance_ms(60_000)
    assert (await limiter.acquire("k")).allowed is True
    assert (await limiter.acquire("k")).allowed is True
    assert (await limiter.acquire("k")).allowed is False


async def test_independent_keys_do_not_share_state(
    redis_client: Redis, clock: _FakeClock, request: pytest.FixtureRequest
) -> None:
    limiter = RedisTokenBucketRateLimiter(
        redis_client,
        rate_per_sec=1.0,
        burst=1,
        key_prefix=_key_prefix(request),
        monotonic_ms=clock,
    )
    assert (await limiter.acquire("tenant-a")).allowed is True
    assert (await limiter.acquire("tenant-b")).allowed is True
    assert (await limiter.acquire("tenant-a")).allowed is False


async def test_cost_greater_than_burst_returns_none_retry(
    redis_client: Redis, clock: _FakeClock, request: pytest.FixtureRequest
) -> None:
    limiter = RedisTokenBucketRateLimiter(
        redis_client,
        rate_per_sec=1.0,
        burst=5,
        key_prefix=_key_prefix(request),
        monotonic_ms=clock,
    )
    decision = await limiter.acquire("k", cost=10)
    assert decision.allowed is False
    assert decision.retry_after_ms is None
    # And the bucket is intact: a normal-cost call still works.
    follow_up = await limiter.acquire("k", cost=5)
    assert follow_up.allowed is True


async def test_shared_redis_state_across_two_limiter_instances(
    redis_client: Redis, clock: _FakeClock, request: pytest.FixtureRequest
) -> None:
    # Two limiter objects, same Redis key: the second one must see
    # the bucket the first one drained. This is the whole point of
    # the Redis backend over the in-memory one.
    prefix = _key_prefix(request)
    a = RedisTokenBucketRateLimiter(
        redis_client, rate_per_sec=1.0, burst=2, key_prefix=prefix, monotonic_ms=clock
    )
    b = RedisTokenBucketRateLimiter(
        redis_client, rate_per_sec=1.0, burst=2, key_prefix=prefix, monotonic_ms=clock
    )
    assert (await a.acquire("k")).allowed is True
    assert (await a.acquire("k")).allowed is True
    assert (await b.acquire("k")).allowed is False
