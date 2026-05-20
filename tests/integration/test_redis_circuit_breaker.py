"""End-to-end :class:`RedisCircuitBreaker` against a real Redis.

Unit tests in ``tests/infra/circuitbreaker/test_redis_breaker.py``
exercise the *wrapper* logic with faked Lua scripts. This module goes
one level deeper: the actual gate + record Lua paths run inside Redis
so any drift between the documented contract and the script's
behaviour is caught immediately.

Each test injects a deterministic monotonic-ms clock so cooldown /
window boundaries are exact and the suite stays fast (no real
``asyncio.sleep`` between phases). Per-test key prefix gives clean
isolation across parallel runs.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from redis.asyncio import Redis

from meta_agent.core.ports.circuit_breaker import CircuitBreakerOpenError
from meta_agent.infra.circuitbreaker.redis_breaker import RedisCircuitBreaker


class _FakeClock:
    """Monotonic-ms source whose value the test advances explicitly."""

    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self.now_ms = start_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance_ms(self, delta: int) -> None:
        self.now_ms += delta


def _key_prefix(request: pytest.FixtureRequest) -> str:
    return f"cb:test:{request.node.name}:"


@pytest.fixture
def clock() -> Iterator[_FakeClock]:
    yield _FakeClock()


async def _fail(msg: str = "boom") -> None:
    raise RuntimeError(msg)


async def _ok() -> str:
    return "ok"


async def test_closed_forwards_calls_and_records_failures(
    redis_client: Redis, clock: _FakeClock, request: pytest.FixtureRequest
) -> None:
    breaker = RedisCircuitBreaker(
        redis_client,
        failure_threshold=3,
        window_seconds=30.0,
        cooldown_seconds=30.0,
        key_prefix=_key_prefix(request),
        monotonic_ms=clock,
    )
    # 2 failures stay below threshold: breaker stays CLOSED.
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call("k", _fail)
    # Successful call must still be forwarded.
    assert await breaker.call("k", _ok) == "ok"


async def test_trips_open_after_threshold_and_fails_fast(
    redis_client: Redis, clock: _FakeClock, request: pytest.FixtureRequest
) -> None:
    breaker = RedisCircuitBreaker(
        redis_client,
        failure_threshold=3,
        window_seconds=30.0,
        cooldown_seconds=30.0,
        key_prefix=_key_prefix(request),
        monotonic_ms=clock,
    )
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await breaker.call("k", _fail)
    # 4th call must fail fast without invoking fn.
    invoked = False

    async def _probe() -> None:
        nonlocal invoked
        invoked = True

    with pytest.raises(CircuitBreakerOpenError) as excinfo:
        await breaker.call("k", _probe)
    assert excinfo.value.key == "k"
    assert excinfo.value.retry_after_ms is not None and excinfo.value.retry_after_ms > 0
    assert invoked is False


async def test_failures_outside_window_do_not_count(
    redis_client: Redis, clock: _FakeClock, request: pytest.FixtureRequest
) -> None:
    breaker = RedisCircuitBreaker(
        redis_client,
        failure_threshold=3,
        window_seconds=10.0,
        cooldown_seconds=30.0,
        key_prefix=_key_prefix(request),
        monotonic_ms=clock,
    )
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call("k", _fail)
    # Advance past the window so the first two failures fall off.
    clock.advance_ms(11_000)
    with pytest.raises(RuntimeError):
        await breaker.call("k", _fail)
    # Only 1 in-window failure ⇒ breaker still CLOSED.
    assert await breaker.call("k", _ok) == "ok"


async def test_half_open_probe_success_restores_closed(
    redis_client: Redis, clock: _FakeClock, request: pytest.FixtureRequest
) -> None:
    breaker = RedisCircuitBreaker(
        redis_client,
        failure_threshold=2,
        window_seconds=30.0,
        cooldown_seconds=5.0,
        key_prefix=_key_prefix(request),
        monotonic_ms=clock,
    )
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call("k", _fail)
    # Inside cooldown ⇒ fail-fast.
    with pytest.raises(CircuitBreakerOpenError):
        await breaker.call("k", _ok)
    clock.advance_ms(5_010)
    # First post-cooldown call is the probe; success ⇒ CLOSED.
    assert await breaker.call("k", _ok) == "ok"
    # Subsequent failure counts from zero.
    with pytest.raises(RuntimeError):
        await breaker.call("k", _fail)


async def test_half_open_probe_failure_reopens_with_fresh_cooldown(
    redis_client: Redis, clock: _FakeClock, request: pytest.FixtureRequest
) -> None:
    breaker = RedisCircuitBreaker(
        redis_client,
        failure_threshold=2,
        window_seconds=30.0,
        cooldown_seconds=5.0,
        key_prefix=_key_prefix(request),
        monotonic_ms=clock,
    )
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call("k", _fail)
    clock.advance_ms(5_010)
    # Probe fails ⇒ OPEN again with a fresh cooldown.
    with pytest.raises(RuntimeError):
        await breaker.call("k", _fail)
    with pytest.raises(CircuitBreakerOpenError):
        await breaker.call("k", _ok)
    clock.advance_ms(5_010)
    # Probe succeeds ⇒ back to CLOSED.
    assert await breaker.call("k", _ok) == "ok"


async def test_keys_are_isolated(
    redis_client: Redis, clock: _FakeClock, request: pytest.FixtureRequest
) -> None:
    breaker = RedisCircuitBreaker(
        redis_client,
        failure_threshold=2,
        window_seconds=30.0,
        cooldown_seconds=30.0,
        key_prefix=_key_prefix(request),
        monotonic_ms=clock,
    )
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call("k-a", _fail)
    # 'k-a' is OPEN, but 'k-b' must still be CLOSED.
    with pytest.raises(CircuitBreakerOpenError):
        await breaker.call("k-a", _ok)
    assert await breaker.call("k-b", _ok) == "ok"


async def test_shared_state_across_two_breaker_instances(
    redis_client: Redis, clock: _FakeClock, request: pytest.FixtureRequest
) -> None:
    # Two breaker objects, same Redis keys: the second one must see
    # the OPEN state the first one tripped. This is the whole point of
    # the Redis backend over the in-memory one.
    prefix = _key_prefix(request)
    a = RedisCircuitBreaker(
        redis_client,
        failure_threshold=2,
        window_seconds=30.0,
        cooldown_seconds=30.0,
        key_prefix=prefix,
        monotonic_ms=clock,
    )
    b = RedisCircuitBreaker(
        redis_client,
        failure_threshold=2,
        window_seconds=30.0,
        cooldown_seconds=30.0,
        key_prefix=prefix,
        monotonic_ms=clock,
    )
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await a.call("k", _fail)
    # Instance ``b`` shares the Redis keys and must observe OPEN too.
    with pytest.raises(CircuitBreakerOpenError):
        await b.call("k", _ok)


async def test_concurrent_callers_in_half_open_get_one_probe(
    redis_client: Redis, clock: _FakeClock, request: pytest.FixtureRequest
) -> None:
    import asyncio

    breaker = RedisCircuitBreaker(
        redis_client,
        failure_threshold=2,
        window_seconds=30.0,
        cooldown_seconds=5.0,
        key_prefix=_key_prefix(request),
        monotonic_ms=clock,
    )
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call("k", _fail)
    clock.advance_ms(5_010)

    # One slow probe + N fast competitors. The slow probe is the call
    # that ``_gate`` picks; competitors must see CircuitBreakerOpenError
    # with ``retry_after_ms is None`` (the probe-in-flight sentinel).
    probe_started = asyncio.Event()
    probe_release = asyncio.Event()

    async def _probe() -> str:
        probe_started.set()
        await probe_release.wait()
        return "probe-ok"

    async def _competitor() -> str:
        return "should-not-run"

    probe_task = asyncio.create_task(breaker.call("k", _probe))
    await probe_started.wait()

    rejected = 0
    for _ in range(3):
        with pytest.raises(CircuitBreakerOpenError) as excinfo:
            await breaker.call("k", _competitor)
        assert excinfo.value.retry_after_ms is None
        rejected += 1
    assert rejected == 3

    probe_release.set()
    assert await probe_task == "probe-ok"

    assert await breaker.call("k", _ok) == "ok"
