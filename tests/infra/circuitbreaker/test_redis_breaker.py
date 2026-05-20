"""Unit tests for :class:`RedisCircuitBreaker`.

The Lua state machine itself is verified end-to-end against a real Redis
in ``tests/integration/test_redis_circuit_breaker.py``. These unit
tests pin the *wrapper* contract:

* ``register_script`` is called twice at construction (gate + record).
* ``call`` dispatches to gate first, then ``fn``, then record.
* Gate decoding maps ``pass`` / ``probe`` / ``open`` correctly and
  ``retry_ms == -1`` decodes to ``None``.
* :class:`RedisError` from either script becomes
  :class:`CircuitBreakerBackendError`.
* ``should_count`` predicate + cancellation handling pass through the
  outcome arguments to the record script.
* Key prefix concatenation produces ``{prefix}{key}:state`` / ``:fail``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from redis.exceptions import RedisError

from meta_agent.core.ports.circuit_breaker import (
    CircuitBreakerBackendError,
    CircuitBreakerOpenError,
)
from meta_agent.infra.circuitbreaker.redis_breaker import RedisCircuitBreaker


class _FakeScript:
    """Stand-in for ``redis.commands.core.AsyncScript``.

    Returns the next entry from ``queue`` on each call; if ``queue`` is
    exhausted, returns the last entry. Captures keys/args for assertions.
    """

    def __init__(self, queue: list[Any]) -> None:
        self._queue = list(queue)
        self.last_keys: list[str] | None = None
        self.last_args: list[Any] | None = None
        self.calls: int = 0

    async def __call__(self, *, keys: list[str], args: list[Any]) -> Any:
        self.last_keys = list(keys)
        self.last_args = list(args)
        self.calls += 1
        if not self._queue:
            return [b"pass", 0]
        if len(self._queue) > 1:
            return self._queue.pop(0)
        return self._queue[0]


def _build_client(
    gate: _FakeScript | Exception,
    record: _FakeScript | Exception,
) -> MagicMock:
    client = MagicMock()
    scripts: list[Any] = []
    for entry in (gate, record):
        if isinstance(entry, Exception):

            async def _raise(*, exc: Exception = entry, **_kw: Any) -> Any:
                raise exc

            scripts.append(_raise)
        else:
            scripts.append(entry)
    client.register_script = MagicMock(side_effect=scripts)
    return client


def test_construction_rejects_invalid_parameters() -> None:
    client = _build_client(_FakeScript([[b"pass", 0]]), _FakeScript([0]))
    with pytest.raises(ValueError, match="failure_threshold"):
        RedisCircuitBreaker(client, failure_threshold=0)
    with pytest.raises(ValueError, match="window_seconds"):
        RedisCircuitBreaker(client, window_seconds=0)
    with pytest.raises(ValueError, match="cooldown_seconds"):
        RedisCircuitBreaker(client, cooldown_seconds=0)


def test_construction_registers_both_scripts() -> None:
    client = _build_client(_FakeScript([[b"pass", 0]]), _FakeScript([0]))
    RedisCircuitBreaker(client)
    assert client.register_script.call_count == 2
    bodies = [c.args[0] for c in client.register_script.call_args_list]
    # First registered script is the gate, second is the record.
    assert "half_open" in bodies[0]
    assert "RPUSH" in bodies[1]


async def test_pass_decision_forwards_to_fn_and_records_success() -> None:
    gate = _FakeScript([[b"pass", 0]])
    record = _FakeScript([0])
    client = _build_client(gate, record)
    breaker = RedisCircuitBreaker(client, key_prefix="cb:", monotonic_ms=lambda: 1_700_000_000_000)

    invoked = 0

    async def _ok() -> str:
        nonlocal invoked
        invoked += 1
        return "ok"

    assert await breaker.call("k", _ok) == "ok"
    assert invoked == 1
    assert gate.last_keys == ["cb:k:state", "cb:k:fail"]
    # Record args: now_ms, window_ms, threshold, success=1, counted=0, is_probe=0, ttl_sec.
    assert record.last_args is not None
    assert record.last_args[3] == "1"
    assert record.last_args[4] == "0"
    assert record.last_args[5] == "0"


async def test_open_decision_raises_with_retry_hint_and_skips_fn() -> None:
    gate = _FakeScript([[b"open", 750]])
    record = _FakeScript([0])
    client = _build_client(gate, record)
    breaker = RedisCircuitBreaker(client, monotonic_ms=lambda: 0)

    async def _never() -> None:
        raise AssertionError("fn must not be invoked when breaker is OPEN")

    with pytest.raises(CircuitBreakerOpenError) as excinfo:
        await breaker.call("k", _never)
    assert excinfo.value.key == "k"
    assert excinfo.value.retry_after_ms == 750
    # Record script must not be touched on the open path.
    assert record.calls == 0


async def test_open_with_no_retry_hint_maps_to_none() -> None:
    # ``probe_in_flight`` collision returns retry_ms == -1.
    gate = _FakeScript([[b"open", -1]])
    record = _FakeScript([0])
    client = _build_client(gate, record)
    breaker = RedisCircuitBreaker(client, monotonic_ms=lambda: 0)

    async def _never() -> None:
        raise AssertionError("fn must not be invoked")

    with pytest.raises(CircuitBreakerOpenError) as excinfo:
        await breaker.call("k", _never)
    assert excinfo.value.retry_after_ms is None


async def test_probe_decision_marks_record_as_probe() -> None:
    gate = _FakeScript([[b"probe", 0]])
    record = _FakeScript([0])
    client = _build_client(gate, record)
    breaker = RedisCircuitBreaker(client, monotonic_ms=lambda: 0)

    async def _ok() -> str:
        return "ok"

    await breaker.call("k", _ok)
    assert record.last_args is not None
    # is_probe flag must be set to "1" so the Lua side resets state to CLOSED.
    assert record.last_args[5] == "1"


async def test_failure_path_records_counted_flag_and_reraises() -> None:
    gate = _FakeScript([[b"pass", 0]])
    record = _FakeScript([0])
    client = _build_client(gate, record)
    breaker = RedisCircuitBreaker(client, monotonic_ms=lambda: 0)

    async def _boom() -> None:
        raise RuntimeError("downstream blew up")

    with pytest.raises(RuntimeError, match="downstream blew up"):
        await breaker.call("k", _boom)
    assert record.last_args is not None
    # success=0, counted=1 (no predicate, default counts everything), is_probe=0.
    assert record.last_args[3] == "0"
    assert record.last_args[4] == "1"
    assert record.last_args[5] == "0"


async def test_should_count_predicate_can_exclude_failure() -> None:
    gate = _FakeScript([[b"pass", 0]])
    record = _FakeScript([0])
    client = _build_client(gate, record)
    breaker = RedisCircuitBreaker(client, monotonic_ms=lambda: 0)

    async def _boom() -> None:
        raise ValueError("caller bug")

    def _count(exc: BaseException) -> bool:
        return not isinstance(exc, ValueError)

    with pytest.raises(ValueError):
        await breaker.call("k", _boom, should_count=_count)
    assert record.last_args is not None
    # success=0, counted=0 (predicate said False), is_probe=0.
    assert record.last_args[3] == "0"
    assert record.last_args[4] == "0"


async def test_cancellation_does_not_count_as_failure() -> None:
    gate = _FakeScript([[b"pass", 0]])
    record = _FakeScript([0])
    client = _build_client(gate, record)
    breaker = RedisCircuitBreaker(client, monotonic_ms=lambda: 0)

    async def _cancel() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await breaker.call("k", _cancel)
    assert record.last_args is not None
    # CancelledError is hard-excluded by the wrapper regardless of predicate.
    assert record.last_args[4] == "0"


async def test_redis_error_in_gate_becomes_backend_error() -> None:
    client = _build_client(RedisError("ECONNRESET"), _FakeScript([0]))
    breaker = RedisCircuitBreaker(client, monotonic_ms=lambda: 0)

    async def _never() -> None:
        raise AssertionError("fn must not run if gate raised")

    with pytest.raises(CircuitBreakerBackendError, match=r"redis EVAL \(gate\)"):
        await breaker.call("k", _never)


async def test_redis_error_in_record_becomes_backend_error() -> None:
    gate = _FakeScript([[b"pass", 0]])
    client = _build_client(gate, RedisError("link down"))
    breaker = RedisCircuitBreaker(client, monotonic_ms=lambda: 0)

    async def _ok() -> str:
        return "ok"

    with pytest.raises(CircuitBreakerBackendError, match=r"redis EVAL \(record\)"):
        await breaker.call("k", _ok)


async def test_string_decision_decodes_when_lua_returns_str_not_bytes() -> None:
    # Some redis-py serializers can return strings instead of bytes for
    # Lua string returns; the wrapper must handle both.
    gate = _FakeScript([["pass", 0]])
    record = _FakeScript([0])
    client = _build_client(gate, record)
    breaker = RedisCircuitBreaker(client, monotonic_ms=lambda: 0)

    async def _ok() -> str:
        return "ok"

    assert await breaker.call("k", _ok) == "ok"


async def test_empty_prefix_does_not_inject_extra_separator() -> None:
    gate = _FakeScript([[b"pass", 0]])
    record = _FakeScript([0])
    client = _build_client(gate, record)
    breaker = RedisCircuitBreaker(client, monotonic_ms=lambda: 0)

    async def _ok() -> str:
        return "ok"

    await breaker.call("llm:openrouter:tenant=t-1:model=m", _ok)
    assert gate.last_keys == [
        "llm:openrouter:tenant=t-1:model=m:state",
        "llm:openrouter:tenant=t-1:model=m:fail",
    ]


async def test_close_does_not_touch_client() -> None:
    # The breaker does not own the client; close() must be a safe no-op
    # so callers can release the shared Redis pool independently.
    gate = _FakeScript([[b"pass", 0]])
    record = _FakeScript([0])
    client = _build_client(gate, record)
    breaker = RedisCircuitBreaker(client)
    await breaker.close()
    # aclose / close on the client must not be called.
    assert not any(
        call_name in {"aclose", "close"} for call_name in (c[0] for c in client.method_calls)
    )
