"""Redis-backed cross-replica :class:`CircuitBreaker`.

Each breaker key occupies two Redis structures under ``{prefix}{key}:``:

* ``state`` (HASH) holds ``state`` (``closed`` / ``open`` / ``half_open``),
  ``opened_at_ms`` (epoch ms stamped on the transition to OPEN), and
  ``probe_in_flight`` (``"0"`` / ``"1"``) — these are the three knobs
  the gate script reads and the record script writes.
* ``fail`` (LIST) is the in-window failure log; ``RPUSH`` on each
  counted failure, the head is pruned with ``LPOP`` while it is older
  than ``now_ms - window_ms``. ``LLEN`` after pruning is the trip
  predicate.

Two short Lua scripts make the state machine atomic across replicas:

* ``_gate`` decides whether the call proceeds. Possible returns are
  ``("pass", 0)``, ``("open", retry_ms)``, ``("probe", 0)``. The
  CLOSED → OPEN → HALF_OPEN → OPEN/CLOSED transitions live entirely
  inside the script so two replicas cannot both believe they are the
  probe at the same time.
* ``_record`` applies the outcome. Probe success clears the failure
  log + resets the state to CLOSED; probe failure stamps a fresh
  ``opened_at_ms``. Non-probe failures (when ``counted=1``) append
  to the failure log, prune stale entries, and trip OPEN if the count
  ≥ threshold.

Time source: monotonic ms is computed **client-side** (same convention
as :class:`RedisTokenBucketRateLimiter`) so the algorithm does not
depend on Redis' server clock and unit tests can inject a fake clock.

:class:`redis.exceptions.RedisError` (or any Lua error) is wrapped in
:class:`CircuitBreakerBackendError`; callers decide fail-open /
fail-closed exactly like the rate-limiter path.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from typing import Final, TypeVar

from redis.asyncio import Redis
from redis.exceptions import RedisError

from meta_agent.core.ports.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerBackendError,
    CircuitBreakerOpenError,
)

T = TypeVar("T")

_DEFAULT_FAILURE_THRESHOLD: Final[int] = 5
_DEFAULT_WINDOW_SECONDS: Final[float] = 30.0
_DEFAULT_COOLDOWN_SECONDS: Final[float] = 30.0

# Decide whether the call may proceed. Atomically transitions
# OPEN → HALF_OPEN when the cooldown has elapsed; the call that
# triggers that transition is taken as the probe. Concurrent callers
# while a probe is in flight are rejected with retry_ms = -1 (no hint).
_GATE_SCRIPT: Final[str] = """
local now_ms = tonumber(ARGV[1])
local cooldown_ms = tonumber(ARGV[2])
local data = redis.call('HMGET', KEYS[1], 'state', 'opened_at_ms', 'probe_in_flight')
local state = data[1]
if not state or state == false then
  return {'pass', 0}
end
if state == 'closed' then
  return {'pass', 0}
end
if state == 'open' then
  local opened_at = tonumber(data[2]) or 0
  local elapsed = now_ms - opened_at
  if elapsed < cooldown_ms then
    local retry = cooldown_ms - elapsed
    if retry < 1 then retry = 1 end
    return {'open', retry}
  end
  redis.call('HSET', KEYS[1], 'state', 'half_open', 'probe_in_flight', '1')
  return {'probe', 0}
end
-- state == half_open
if data[3] == '1' then
  return {'open', -1}
end
redis.call('HSET', KEYS[1], 'probe_in_flight', '1')
return {'probe', 0}
"""

# Apply a call outcome to the breaker. Failure ZSET is replaced with a
# LIST keyed on ``now_ms`` for cheap RPUSH + head-prune semantics.
_RECORD_SCRIPT: Final[str] = """
local now_ms = tonumber(ARGV[1])
local window_ms = tonumber(ARGV[2])
local threshold = tonumber(ARGV[3])
local success = ARGV[4] == '1'
local counted = ARGV[5] == '1'
local is_probe = ARGV[6] == '1'
local ttl_sec = tonumber(ARGV[7])

if is_probe then
  if success then
    redis.call('HSET', KEYS[1], 'state', 'closed', 'opened_at_ms', '0', 'probe_in_flight', '0')
    redis.call('DEL', KEYS[2])
  else
    redis.call('HSET', KEYS[1], 'state', 'open', 'opened_at_ms', tostring(now_ms),
               'probe_in_flight', '0')
    redis.call('DEL', KEYS[2])
  end
  redis.call('EXPIRE', KEYS[1], ttl_sec)
  return 0
end

if success or not counted then
  return 0
end

redis.call('RPUSH', KEYS[2], tostring(now_ms))
local cutoff = now_ms - window_ms
while true do
  local oldest = redis.call('LINDEX', KEYS[2], 0)
  if not oldest or oldest == false then break end
  if tonumber(oldest) < cutoff then
    redis.call('LPOP', KEYS[2])
  else
    break
  end
end
local count = redis.call('LLEN', KEYS[2])
if count >= threshold then
  redis.call('HSET', KEYS[1], 'state', 'open', 'opened_at_ms', tostring(now_ms),
             'probe_in_flight', '0')
  redis.call('DEL', KEYS[2])
end
redis.call('EXPIRE', KEYS[1], ttl_sec)
redis.call('EXPIRE', KEYS[2], ttl_sec)
return count
"""


class RedisCircuitBreaker(CircuitBreaker):
    """Cross-replica breaker sharing state through Redis.

    The Redis client lifecycle is owned by the caller (the same pool
    used by the message-queue and rate-limiter adapters); :meth:`close`
    is a no-op so the breaker does not accidentally tear down a shared
    connection.
    """

    def __init__(
        self,
        client: Redis,
        *,
        failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
        window_seconds: float = _DEFAULT_WINDOW_SECONDS,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
        key_prefix: str = "",
        monotonic_ms: Callable[[], int] | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be > 0")
        self._client = client
        self._failure_threshold = failure_threshold
        self._window_ms = int(window_seconds * 1000)
        self._cooldown_ms = int(cooldown_seconds * 1000)
        self._prefix = key_prefix
        self._monotonic_ms = monotonic_ms if monotonic_ms is not None else _default_now_ms
        self._gate_script = client.register_script(_GATE_SCRIPT)
        self._record_script = client.register_script(_RECORD_SCRIPT)
        # TTL must outlive at least one full window + cooldown so a
        # quiet replica can still observe the OPEN state on probe.
        self._ttl_sec = max(1, math.ceil((window_seconds + cooldown_seconds) * 2))

    def _keys(self, key: str) -> tuple[str, str]:
        scoped = f"{self._prefix}{key}" if self._prefix else key
        return f"{scoped}:state", f"{scoped}:fail"

    async def call(
        self,
        key: str,
        fn: Callable[[], Awaitable[T]],
        *,
        should_count: Callable[[BaseException], bool] | None = None,
    ) -> T:
        state_key, fail_key = self._keys(key)
        is_probe = await self._gate(key, state_key, fail_key)
        try:
            result = await fn()
        except BaseException as exc:
            counted = self._should_count(exc, should_count)
            await self._record(
                state_key, fail_key, success=False, counted=counted, is_probe=is_probe
            )
            raise
        await self._record(state_key, fail_key, success=True, counted=False, is_probe=is_probe)
        return result

    @staticmethod
    def _should_count(
        exc: BaseException,
        predicate: Callable[[BaseException], bool] | None,
    ) -> bool:
        if isinstance(exc, asyncio.CancelledError):
            return False
        if predicate is None:
            return True
        return predicate(exc)

    async def _gate(self, key: str, state_key: str, fail_key: str) -> bool:
        now_ms = self._monotonic_ms()
        try:
            raw = await self._gate_script(
                keys=[state_key, fail_key],
                args=[now_ms, self._cooldown_ms],
            )
        except RedisError as exc:
            raise CircuitBreakerBackendError(f"redis EVAL (gate) failed: {exc}") from exc
        decision = raw[0].decode() if isinstance(raw[0], bytes) else str(raw[0])
        retry_ms = int(raw[1])
        if decision == "pass":
            return False
        if decision == "probe":
            return True
        # ``open``
        raise CircuitBreakerOpenError(
            f"circuit breaker open for {key!r}",
            key=key,
            retry_after_ms=None if retry_ms < 0 else retry_ms,
        )

    async def _record(
        self,
        state_key: str,
        fail_key: str,
        *,
        success: bool,
        counted: bool,
        is_probe: bool,
    ) -> None:
        now_ms = self._monotonic_ms()
        try:
            await self._record_script(
                keys=[state_key, fail_key],
                args=[
                    now_ms,
                    self._window_ms,
                    self._failure_threshold,
                    "1" if success else "0",
                    "1" if counted else "0",
                    "1" if is_probe else "0",
                    self._ttl_sec,
                ],
            )
        except RedisError as exc:
            raise CircuitBreakerBackendError(f"redis EVAL (record) failed: {exc}") from exc


def _default_now_ms() -> int:
    return math.floor(time.monotonic() * 1000)


__all__ = ["RedisCircuitBreaker"]
