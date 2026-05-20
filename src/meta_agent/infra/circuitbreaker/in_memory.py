"""Single-process :class:`CircuitBreaker` with a rolling failure window.

Semantics
=========

Per-key state machine ``CLOSED → OPEN → HALF_OPEN → CLOSED``:

* ``CLOSED``    — ``fn`` runs; failures are appended to a per-key deque
  with their timestamps. When the deque holds ``failure_threshold`` or
  more entries within the trailing ``window_seconds``, the state flips
  to ``OPEN`` and ``opened_at`` is stamped.
* ``OPEN``      — calls fail fast with :class:`CircuitBreakerOpenError`
  carrying ``retry_after_ms`` until ``opened_at + cooldown_seconds``
  has elapsed; the next call after that triggers a transition to
  ``HALF_OPEN`` and itself is taken as the probe.
* ``HALF_OPEN`` — exactly one probe call is permitted; concurrent
  callers receive :class:`CircuitBreakerOpenError` until the probe
  resolves. Probe success → ``CLOSED`` with cleared counters; probe
  failure → ``OPEN`` with a fresh ``opened_at``.

``should_count`` is consulted on every exception raised by ``fn``;
returning ``False`` means "this isn't a downstream fault" and the
exception passes through without contributing to the failure counter
(typical use: exclude validation errors, caller-side cancellations,
and inner-rate-limit denials).

This adapter is single-process only — :mod:`asyncio.Lock` serialises
state transitions per key. The cross-replica shared-state version
arrives in a follow-up adapter that backs the counters in Redis.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final, TypeVar

from meta_agent.core.ports.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitBreakerState,
)

T = TypeVar("T")

_DEFAULT_FAILURE_THRESHOLD: Final[int] = 5
_DEFAULT_WINDOW_SECONDS: Final[float] = 30.0
_DEFAULT_COOLDOWN_SECONDS: Final[float] = 30.0


@dataclass
class _BucketState:
    """Per-key breaker state. Only mutated under ``lock``."""

    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    failures: deque[float] = field(default_factory=deque)
    opened_at: float | None = None
    probe_in_flight: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class InMemoryCircuitBreaker(CircuitBreaker):
    """Cooperative breaker keyed by opaque string, single-process scope."""

    def __init__(
        self,
        *,
        failure_threshold: int = _DEFAULT_FAILURE_THRESHOLD,
        window_seconds: float = _DEFAULT_WINDOW_SECONDS,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be > 0")
        self._failure_threshold = failure_threshold
        self._window_seconds = window_seconds
        self._cooldown_seconds = cooldown_seconds
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._buckets: dict[str, _BucketState] = {}
        self._registry_lock = asyncio.Lock()

    async def call(
        self,
        key: str,
        fn: Callable[[], Awaitable[T]],
        *,
        should_count: Callable[[BaseException], bool] | None = None,
    ) -> T:
        bucket = await self._get_bucket(key)
        is_probe = await self._gate(key, bucket)
        try:
            result = await fn()
        except BaseException as exc:
            counted = self._should_count(exc, should_count)
            await self._record_outcome(bucket, success=False, counted=counted, is_probe=is_probe)
            raise
        await self._record_outcome(bucket, success=True, counted=False, is_probe=is_probe)
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

    async def _get_bucket(self, key: str) -> _BucketState:
        async with self._registry_lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _BucketState()
                self._buckets[key] = bucket
            return bucket

    async def _gate(self, key: str, bucket: _BucketState) -> bool:
        """Decide whether the call may proceed. Returns ``True`` for probes."""
        async with bucket.lock:
            now = self._monotonic()
            if bucket.state is CircuitBreakerState.CLOSED:
                return False
            if bucket.state is CircuitBreakerState.OPEN:
                assert bucket.opened_at is not None
                elapsed = now - bucket.opened_at
                if elapsed < self._cooldown_seconds:
                    retry_ms = max(1, int((self._cooldown_seconds - elapsed) * 1000))
                    raise CircuitBreakerOpenError(
                        f"circuit breaker open for {key!r}",
                        key=key,
                        retry_after_ms=retry_ms,
                    )
                bucket.state = CircuitBreakerState.HALF_OPEN
                bucket.probe_in_flight = True
                return True
            # HALF_OPEN: a probe is in flight; reject concurrent callers.
            raise CircuitBreakerOpenError(
                f"circuit breaker probe in flight for {key!r}",
                key=key,
                retry_after_ms=None,
            )

    async def _record_outcome(
        self,
        bucket: _BucketState,
        *,
        success: bool,
        counted: bool,
        is_probe: bool,
    ) -> None:
        async with bucket.lock:
            now = self._monotonic()
            if is_probe:
                bucket.probe_in_flight = False
                if success:
                    bucket.state = CircuitBreakerState.CLOSED
                    bucket.failures.clear()
                    bucket.opened_at = None
                else:
                    bucket.state = CircuitBreakerState.OPEN
                    bucket.opened_at = now
                    bucket.failures.clear()
                return
            if success or not counted:
                return
            bucket.failures.append(now)
            cutoff = now - self._window_seconds
            while bucket.failures and bucket.failures[0] < cutoff:
                bucket.failures.popleft()
            if len(bucket.failures) >= self._failure_threshold:
                bucket.state = CircuitBreakerState.OPEN
                bucket.opened_at = now


__all__ = ["InMemoryCircuitBreaker"]
