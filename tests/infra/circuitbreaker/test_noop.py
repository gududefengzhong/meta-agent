"""Unit tests for :class:`NoopCircuitBreaker`."""

from __future__ import annotations

import pytest

from meta_agent.infra.circuitbreaker.noop import NoopCircuitBreaker


async def test_forwards_call_unconditionally() -> None:
    breaker = NoopCircuitBreaker()
    calls = 0

    async def _fn() -> str:
        nonlocal calls
        calls += 1
        return "ok"

    assert await breaker.call("k", _fn) == "ok"
    assert calls == 1


async def test_propagates_inner_exception_without_recording() -> None:
    breaker = NoopCircuitBreaker()

    async def _boom() -> None:
        raise RuntimeError("nope")

    # Even after several failures the breaker never trips: every call
    # is still forwarded.
    for _ in range(10):
        with pytest.raises(RuntimeError):
            await breaker.call("k", _boom)
    # The 11th call must still be forwarded (would have tripped a real
    # breaker by now).
    with pytest.raises(RuntimeError):
        await breaker.call("k", _boom)


async def test_should_count_predicate_is_ignored() -> None:
    breaker = NoopCircuitBreaker()
    seen: list[BaseException] = []

    def _count(exc: BaseException) -> bool:
        seen.append(exc)
        return True

    async def _boom() -> None:
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        await breaker.call("k", _boom, should_count=_count)
    # NoOp ignores the predicate entirely; predicate must not be invoked.
    assert seen == []


async def test_close_is_noop() -> None:
    breaker = NoopCircuitBreaker()
    await breaker.close()
