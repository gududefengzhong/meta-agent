"""Env-driven configuration + factory for :class:`CircuitBreaker`.

Picks one of:

* ``noop``   — :class:`NoopCircuitBreaker` (default; current behaviour unchanged)
* ``memory`` — :class:`InMemoryCircuitBreaker` (single-process; dev / smoke)
* ``redis``  — :class:`RedisCircuitBreaker` (cross-replica; production)

Env variables
=============

========================================== ===============================
``META_AGENT_CIRCUITBREAKER_BACKEND``      ``noop`` / ``memory`` / ``redis``
``META_AGENT_CIRCUITBREAKER_FAILURE_THRESHOLD`` Failures in window before tripping (default ``5``)
``META_AGENT_CIRCUITBREAKER_WINDOW_SECONDS`` Sliding failure window in seconds (default ``30``)
``META_AGENT_CIRCUITBREAKER_COOLDOWN_SECONDS`` OPEN cooldown before probe in seconds (default ``30``)
``META_AGENT_CIRCUITBREAKER_KEY_PREFIX``   Optional Redis namespace prefix (default empty)
========================================== ===============================

Defaults match the in-memory adapter so swapping ``noop`` → ``memory`` →
``redis`` is observably a no-op at the same parameter values. Production
overrides the thresholds based on observed downstream behaviour.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final, Literal

from redis.asyncio import Redis

from meta_agent.core.ports.circuit_breaker import CircuitBreaker
from meta_agent.infra.circuitbreaker.in_memory import InMemoryCircuitBreaker
from meta_agent.infra.circuitbreaker.noop import NoopCircuitBreaker
from meta_agent.infra.circuitbreaker.redis_breaker import RedisCircuitBreaker

_BACKEND_ENV: Final[str] = "META_AGENT_CIRCUITBREAKER_BACKEND"
_THRESHOLD_ENV: Final[str] = "META_AGENT_CIRCUITBREAKER_FAILURE_THRESHOLD"
_WINDOW_ENV: Final[str] = "META_AGENT_CIRCUITBREAKER_WINDOW_SECONDS"
_COOLDOWN_ENV: Final[str] = "META_AGENT_CIRCUITBREAKER_COOLDOWN_SECONDS"
_PREFIX_ENV: Final[str] = "META_AGENT_CIRCUITBREAKER_KEY_PREFIX"

_DEFAULT_BACKEND: Final[str] = "noop"
_DEFAULT_THRESHOLD: Final[int] = 5
_DEFAULT_WINDOW: Final[float] = 30.0
_DEFAULT_COOLDOWN: Final[float] = 30.0
_DEFAULT_PREFIX: Final[str] = ""

Backend = Literal["noop", "memory", "redis"]
_SUPPORTED_BACKENDS: Final[tuple[Backend, ...]] = ("noop", "memory", "redis")


@dataclass(frozen=True, slots=True)
class CircuitBreakerConfig:
    """Parsed env settings for the circuit-breaker factory."""

    backend: Backend
    failure_threshold: int
    window_seconds: float
    cooldown_seconds: float
    key_prefix: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> CircuitBreakerConfig:
        source: dict[str, str] = dict(env if env is not None else os.environ)
        backend_raw = source.get(_BACKEND_ENV, _DEFAULT_BACKEND).strip().lower()
        if backend_raw not in _SUPPORTED_BACKENDS:
            raise ValueError(f"{_BACKEND_ENV}={backend_raw!r} not in {_SUPPORTED_BACKENDS}")
        threshold_raw = source.get(_THRESHOLD_ENV, str(_DEFAULT_THRESHOLD))
        try:
            threshold = int(threshold_raw)
        except ValueError as exc:
            raise ValueError(f"{_THRESHOLD_ENV}={threshold_raw!r} is not an int") from exc
        if threshold < 1:
            raise ValueError(f"{_THRESHOLD_ENV}={threshold} must be >= 1")
        window_raw = source.get(_WINDOW_ENV, str(_DEFAULT_WINDOW))
        try:
            window = float(window_raw)
        except ValueError as exc:
            raise ValueError(f"{_WINDOW_ENV}={window_raw!r} is not a float") from exc
        if window <= 0:
            raise ValueError(f"{_WINDOW_ENV}={window} must be > 0")
        cooldown_raw = source.get(_COOLDOWN_ENV, str(_DEFAULT_COOLDOWN))
        try:
            cooldown = float(cooldown_raw)
        except ValueError as exc:
            raise ValueError(f"{_COOLDOWN_ENV}={cooldown_raw!r} is not a float") from exc
        if cooldown <= 0:
            raise ValueError(f"{_COOLDOWN_ENV}={cooldown} must be > 0")
        return cls(
            backend=backend_raw,
            failure_threshold=threshold,
            window_seconds=window,
            cooldown_seconds=cooldown,
            key_prefix=source.get(_PREFIX_ENV, _DEFAULT_PREFIX),
        )


def build_circuit_breaker_from_config(
    config: CircuitBreakerConfig,
    *,
    redis_client: Redis | None = None,
) -> CircuitBreaker:
    """Materialise a :class:`CircuitBreaker` from a parsed config.

    Parameters
    ----------
    config:
        Result of :meth:`CircuitBreakerConfig.from_env`.
    redis_client:
        Shared Redis client. Required when ``config.backend == "redis"``;
        ignored otherwise. Callers should pass the same client used by
        the message-queue and rate-limiter adapters so the connection
        pool is shared.

    Raises
    ------
    ValueError
        If ``backend == "redis"`` but no Redis client was provided.
    """

    if config.backend == "noop":
        return NoopCircuitBreaker()
    if config.backend == "memory":
        return InMemoryCircuitBreaker(
            failure_threshold=config.failure_threshold,
            window_seconds=config.window_seconds,
            cooldown_seconds=config.cooldown_seconds,
        )
    if config.backend == "redis":
        if redis_client is None:
            raise ValueError(f"{_BACKEND_ENV}=redis requires a Redis client to be passed in")
        return RedisCircuitBreaker(
            redis_client,
            failure_threshold=config.failure_threshold,
            window_seconds=config.window_seconds,
            cooldown_seconds=config.cooldown_seconds,
            key_prefix=config.key_prefix,
        )
    # mypy: ``Backend`` Literal guarantees we don't reach here.
    raise AssertionError(f"unreachable backend={config.backend!r}")


__all__ = [
    "Backend",
    "CircuitBreakerConfig",
    "build_circuit_breaker_from_config",
]
