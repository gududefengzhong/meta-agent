"""State-machine tests for :class:`InMemoryCircuitBreaker`.

These tests pin the CLOSED → OPEN → HALF_OPEN → CLOSED transitions
that any shared-state adapter (Redis-backed, etc.) must replicate.
Time is injected so we can exercise the cooldown deterministically.
"""

from __future__ import annotations

import asyncio

import pytest

from meta_agent.core.ports.circuit_breaker import CircuitBreakerOpenError
from meta_agent.infra.circuitbreaker.in_memory import InMemoryCircuitBreaker


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _build(
    *,
    threshold: int = 3,
    window: float = 30.0,
    cooldown: float = 30.0,
    clock: _FakeClock | None = None,
) -> tuple[InMemoryCircuitBreaker, _FakeClock]:
    clock = clock or _FakeClock()
    breaker = InMemoryCircuitBreaker(
        failure_threshold=threshold,
        window_seconds=window,
        cooldown_seconds=cooldown,
        monotonic=clock,
    )
    return breaker, clock


async def _fail(_msg: str = "boom") -> None:
    raise RuntimeError(_msg)


async def _ok() -> str:
    return "ok"


def test_construction_rejects_invalid_parameters() -> None:
    with pytest.raises(ValueError, match="failure_threshold"):
        InMemoryCircuitBreaker(failure_threshold=0)
    with pytest.raises(ValueError, match="window_seconds"):
        InMemoryCircuitBreaker(window_seconds=0)
    with pytest.raises(ValueError, match="cooldown_seconds"):
        InMemoryCircuitBreaker(cooldown_seconds=0)


async def test_closed_forwards_calls_and_records_failures() -> None:
    breaker, _ = _build(threshold=3)
    # 2 failures stay below threshold: breaker stays CLOSED.
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call("k", _fail)
    # A successful call must still be forwarded.
    assert await breaker.call("k", _ok) == "ok"


async def test_trips_open_after_threshold_failures() -> None:
    breaker, _ = _build(threshold=3)
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


async def test_failures_outside_window_do_not_count() -> None:
    breaker, clock = _build(threshold=3, window=10.0)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call("k", _fail)
    # Advance past the window so the first two failures fall off.
    clock.advance(11.0)
    with pytest.raises(RuntimeError):
        await breaker.call("k", _fail)
    # Only 1 in-window failure ⇒ breaker still CLOSED.
    assert await breaker.call("k", _ok) == "ok"


async def test_should_count_predicate_excludes_caller_errors() -> None:
    breaker, _ = _build(threshold=2)
    predicate: list[BaseException] = []

    def _count(exc: BaseException) -> bool:
        predicate.append(exc)
        return not isinstance(exc, ValueError)

    async def _value_err() -> None:
        raise ValueError("ignore-me")

    for _ in range(5):
        with pytest.raises(ValueError):
            await breaker.call("k", _value_err, should_count=_count)
    # ValueError is excluded ⇒ never trips even after many failures.
    assert await breaker.call("k", _ok, should_count=_count) == "ok"
    assert len(predicate) == 5


async def test_half_open_probe_success_restores_closed() -> None:
    breaker, clock = _build(threshold=2, cooldown=5.0)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call("k", _fail)
    # Inside cooldown ⇒ fail-fast.
    with pytest.raises(CircuitBreakerOpenError):
        await breaker.call("k", _ok)
    clock.advance(5.01)
    # First post-cooldown call is the probe; success ⇒ CLOSED.
    assert await breaker.call("k", _ok) == "ok"
    # Subsequent failures count from zero (window cleared on close).
    with pytest.raises(RuntimeError):
        await breaker.call("k", _fail)
    # Still CLOSED — only 1 failure < threshold 2.
    assert await breaker.call("k", _ok) == "ok"


async def test_half_open_probe_failure_reopens_with_fresh_cooldown() -> None:
    breaker, clock = _build(threshold=2, cooldown=5.0)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call("k", _fail)
    clock.advance(5.01)
    # Probe fails ⇒ OPEN again with a fresh cooldown.
    with pytest.raises(RuntimeError):
        await breaker.call("k", _fail)
    with pytest.raises(CircuitBreakerOpenError):
        await breaker.call("k", _ok)
    clock.advance(5.01)
    # Probe succeeds ⇒ back to CLOSED.
    assert await breaker.call("k", _ok) == "ok"


async def test_keys_are_isolated() -> None:
    breaker, _ = _build(threshold=2)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await breaker.call("k-a", _fail)
    # 'k-a' is OPEN, but 'k-b' must still be CLOSED.
    with pytest.raises(CircuitBreakerOpenError):
        await breaker.call("k-a", _ok)
    assert await breaker.call("k-b", _ok) == "ok"


async def test_cancellation_does_not_count_as_failure() -> None:
    breaker, _ = _build(threshold=2)

    async def _cancel() -> None:
        raise asyncio.CancelledError

    for _ in range(5):
        with pytest.raises(asyncio.CancelledError):
            await breaker.call("k", _cancel)
    # Cancellation must never trip the breaker.
    assert await breaker.call("k", _ok) == "ok"
