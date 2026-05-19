"""Unit tests for :class:`RedisTokenBucketRateLimiter`.

The Lua algorithm itself is verified end-to-end against a real Redis
in ``tests/integration/test_redis_rate_limiter.py``. These unit tests
only pin the *wrapper* contract:

* ``register_script`` is called once at construction.
* ``acquire`` invokes the script with the right ``KEYS``/``ARGV``.
* Key-prefix concatenation is correct.
* ``RedisError`` is mapped to :class:`RateLimiterBackendError`.
* The Lua-side ``retry_ms=-1`` sentinel maps to ``retry_after_ms=None``.
* Construction-time validation rejects bad params.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import RedisError

from meta_agent.core.ports.rate_limiter import RateLimiterBackendError
from meta_agent.infra.ratelimit.redis_token_bucket import RedisTokenBucketRateLimiter


class _FakeScript:
    """Stand-in for ``redis.commands.core.AsyncScript``.

    Captures the last call so assertions can check KEYS/ARGV without
    parsing real Lua. The return value is whatever the test pre-loads.
    """

    def __init__(self, returns: list[int]) -> None:
        self._returns = returns
        self.last_keys: list[str] | None = None
        self.last_args: list[Any] | None = None
        self.calls: int = 0

    async def __call__(self, *, keys: list[str], args: list[Any]) -> list[int]:
        self.last_keys = list(keys)
        self.last_args = list(args)
        self.calls += 1
        return self._returns


def _build_client(script: _FakeScript | Exception) -> MagicMock:
    client = MagicMock()
    if isinstance(script, Exception):
        # When acquire calls the script, it should raise RedisError.
        async def _raise(**_kw: Any) -> list[int]:
            raise script

        client.register_script = MagicMock(return_value=_raise)
    else:
        client.register_script = MagicMock(return_value=script)
    return client


def test_construction_rejects_invalid_parameters() -> None:
    client = _build_client(_FakeScript([1, 5, 0]))
    with pytest.raises(ValueError, match="rate_per_sec must be > 0"):
        RedisTokenBucketRateLimiter(client, rate_per_sec=0, burst=10)
    with pytest.raises(ValueError, match="burst must be >= 1"):
        RedisTokenBucketRateLimiter(client, rate_per_sec=1.0, burst=0)


def test_construction_registers_script_once() -> None:
    client = _build_client(_FakeScript([1, 5, 0]))
    RedisTokenBucketRateLimiter(client, rate_per_sec=1.0, burst=5)
    client.register_script.assert_called_once()
    # The registered body must be the Lua refill-and-decide script.
    body = client.register_script.call_args.args[0]
    assert "HMGET" in body and "HSET" in body and "EXPIRE" in body


async def test_acquire_passes_key_args_and_clock() -> None:
    script = _FakeScript([1, 4, 0])
    client = _build_client(script)
    clock = iter([1_700_000_000_000])
    limiter = RedisTokenBucketRateLimiter(
        client,
        rate_per_sec=2.0,
        burst=5,
        key_prefix="rl:",
        monotonic_ms=lambda: next(clock),
    )
    decision = await limiter.acquire("tenant=A:model=x", cost=2)
    assert decision.allowed is True
    assert decision.remaining == 4
    assert decision.retry_after_ms is None
    assert script.last_keys == ["rl:tenant=A:model=x"]
    assert script.last_args == [2.0, 5, 2, 1_700_000_000_000]


async def test_empty_prefix_does_not_corrupt_key() -> None:
    script = _FakeScript([1, 0, 0])
    client = _build_client(script)
    limiter = RedisTokenBucketRateLimiter(client, rate_per_sec=1.0, burst=1, monotonic_ms=lambda: 0)
    await limiter.acquire("k")
    assert script.last_keys == ["k"]


async def test_denied_decision_maps_retry_ms() -> None:
    script = _FakeScript([0, 0, 250])
    client = _build_client(script)
    limiter = RedisTokenBucketRateLimiter(client, rate_per_sec=4.0, burst=2, monotonic_ms=lambda: 0)
    decision = await limiter.acquire("k")
    assert decision.allowed is False
    assert decision.retry_after_ms == 250


async def test_cost_exceeds_burst_sentinel_becomes_none() -> None:
    # Lua returns retry_ms=-1 when cost > burst (no retry can succeed).
    script = _FakeScript([0, 0, -1])
    client = _build_client(script)
    limiter = RedisTokenBucketRateLimiter(client, rate_per_sec=1.0, burst=5, monotonic_ms=lambda: 0)
    decision = await limiter.acquire("k", cost=10)
    assert decision.allowed is False
    assert decision.retry_after_ms is None


async def test_redis_error_becomes_backend_error() -> None:
    client = _build_client(RedisError("ECONNRESET"))
    limiter = RedisTokenBucketRateLimiter(client, rate_per_sec=1.0, burst=5, monotonic_ms=lambda: 0)
    with pytest.raises(RateLimiterBackendError, match="redis EVAL failed"):
        await limiter.acquire("k")


async def test_rejects_non_positive_cost() -> None:
    script = _FakeScript([1, 0, 0])
    client = _build_client(script)
    limiter = RedisTokenBucketRateLimiter(client, rate_per_sec=1.0, burst=5, monotonic_ms=lambda: 0)
    with pytest.raises(ValueError, match="cost must be >= 1"):
        await limiter.acquire("k", cost=0)


async def test_close_does_not_touch_client() -> None:
    # The limiter does not own the client; close() must be a safe no-op
    # so callers can release the shared Redis pool independently.
    client = _build_client(_FakeScript([1, 0, 0]))
    client.aclose = AsyncMock()
    limiter = RedisTokenBucketRateLimiter(client, rate_per_sec=1.0, burst=1, monotonic_ms=lambda: 0)
    await limiter.close()
    client.aclose.assert_not_called()
