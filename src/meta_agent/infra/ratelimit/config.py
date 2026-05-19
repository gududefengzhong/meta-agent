"""Env-driven configuration + factory for :class:`RateLimiter`.

Picks one of:

* ``noop``   — :class:`NoopRateLimiter` (default; current behaviour unchanged)
* ``memory`` — :class:`InMemoryTokenBucketRateLimiter` (single-process; dev / smoke)
* ``redis``  — :class:`RedisTokenBucketRateLimiter` (cross-replica; production)

Env variables
=============

================================== ===============================
``META_AGENT_RATELIMIT_BACKEND``   ``noop`` / ``memory`` / ``redis``
``META_AGENT_RATELIMIT_RATE_PER_SEC`` Tokens / second (default ``50.0``)
``META_AGENT_RATELIMIT_BURST``     Bucket capacity (default ``200``)
``META_AGENT_RATELIMIT_KEY_PREFIX`` Optional namespace prefix (default empty)
================================== ===============================

The defaults are deliberately generous so dev and smoke flows do not
need to tune anything; production deployments override rate / burst
based on observed traffic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final, Literal

from redis.asyncio import Redis

from meta_agent.core.ports.rate_limiter import RateLimiter
from meta_agent.infra.ratelimit.in_memory import InMemoryTokenBucketRateLimiter
from meta_agent.infra.ratelimit.noop import NoopRateLimiter
from meta_agent.infra.ratelimit.redis_token_bucket import RedisTokenBucketRateLimiter

_BACKEND_ENV: Final[str] = "META_AGENT_RATELIMIT_BACKEND"
_RATE_ENV: Final[str] = "META_AGENT_RATELIMIT_RATE_PER_SEC"
_BURST_ENV: Final[str] = "META_AGENT_RATELIMIT_BURST"
_PREFIX_ENV: Final[str] = "META_AGENT_RATELIMIT_KEY_PREFIX"

_DEFAULT_BACKEND: Final[str] = "noop"
_DEFAULT_RATE: Final[float] = 50.0
_DEFAULT_BURST: Final[int] = 200
_DEFAULT_PREFIX: Final[str] = ""

Backend = Literal["noop", "memory", "redis"]
_SUPPORTED_BACKENDS: Final[tuple[Backend, ...]] = ("noop", "memory", "redis")


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    """Parsed env settings for the rate-limiter factory."""

    backend: Backend
    rate_per_sec: float
    burst: int
    key_prefix: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> RateLimitConfig:
        source: dict[str, str] = dict(env if env is not None else os.environ)
        backend_raw = source.get(_BACKEND_ENV, _DEFAULT_BACKEND).strip().lower()
        if backend_raw not in _SUPPORTED_BACKENDS:
            raise ValueError(f"{_BACKEND_ENV}={backend_raw!r} not in {_SUPPORTED_BACKENDS}")
        rate_raw = source.get(_RATE_ENV, str(_DEFAULT_RATE))
        try:
            rate = float(rate_raw)
        except ValueError as exc:
            raise ValueError(f"{_RATE_ENV}={rate_raw!r} is not a float") from exc
        if rate <= 0:
            raise ValueError(f"{_RATE_ENV}={rate} must be > 0")
        burst_raw = source.get(_BURST_ENV, str(_DEFAULT_BURST))
        try:
            burst = int(burst_raw)
        except ValueError as exc:
            raise ValueError(f"{_BURST_ENV}={burst_raw!r} is not an int") from exc
        if burst < 1:
            raise ValueError(f"{_BURST_ENV}={burst} must be >= 1")
        return cls(
            backend=backend_raw,
            rate_per_sec=rate,
            burst=burst,
            key_prefix=source.get(_PREFIX_ENV, _DEFAULT_PREFIX),
        )


def build_rate_limiter_from_config(
    config: RateLimitConfig,
    *,
    redis_client: Redis | None = None,
) -> RateLimiter:
    """Materialise a :class:`RateLimiter` from a parsed config.

    Parameters
    ----------
    config:
        Result of :meth:`RateLimitConfig.from_env`.
    redis_client:
        Shared Redis client. Required when ``config.backend == "redis"``;
        ignored otherwise. Callers should pass the same client that the
        message-queue adapters use so connection pooling is shared.

    Raises
    ------
    ValueError
        If ``backend == "redis"`` but no Redis client was provided.
    """

    if config.backend == "noop":
        return NoopRateLimiter()
    if config.backend == "memory":
        return InMemoryTokenBucketRateLimiter(
            rate_per_sec=config.rate_per_sec,
            burst=config.burst,
        )
    if config.backend == "redis":
        if redis_client is None:
            raise ValueError(f"{_BACKEND_ENV}=redis requires a Redis client to be passed in")
        return RedisTokenBucketRateLimiter(
            redis_client,
            rate_per_sec=config.rate_per_sec,
            burst=config.burst,
            key_prefix=config.key_prefix,
        )
    # mypy: ``Backend`` Literal guarantees we don't reach here.
    raise AssertionError(f"unreachable backend={config.backend!r}")


__all__ = [
    "Backend",
    "RateLimitConfig",
    "build_rate_limiter_from_config",
]
