"""Env-parsing + factory tests for :mod:`meta_agent.infra.ratelimit.config`."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from meta_agent.infra.ratelimit.config import (
    RateLimitConfig,
    build_rate_limiter_from_config,
)
from meta_agent.infra.ratelimit.in_memory import InMemoryTokenBucketRateLimiter
from meta_agent.infra.ratelimit.noop import NoopRateLimiter
from meta_agent.infra.ratelimit.redis_token_bucket import RedisTokenBucketRateLimiter


def test_defaults_select_noop_with_documented_numbers() -> None:
    cfg = RateLimitConfig.from_env({})
    assert cfg.backend == "noop"
    assert cfg.rate_per_sec == 50.0
    assert cfg.burst == 200
    assert cfg.key_prefix == ""


def test_explicit_backend_overrides() -> None:
    cfg = RateLimitConfig.from_env(
        {
            "META_AGENT_RATELIMIT_BACKEND": "memory",
            "META_AGENT_RATELIMIT_RATE_PER_SEC": "2.5",
            "META_AGENT_RATELIMIT_BURST": "10",
            "META_AGENT_RATELIMIT_KEY_PREFIX": "stage:",
        }
    )
    assert cfg.backend == "memory"
    assert cfg.rate_per_sec == 2.5
    assert cfg.burst == 10
    assert cfg.key_prefix == "stage:"


def test_backend_case_and_whitespace_insensitive() -> None:
    cfg = RateLimitConfig.from_env({"META_AGENT_RATELIMIT_BACKEND": "  Redis  "})
    assert cfg.backend == "redis"


def test_unknown_backend_rejected() -> None:
    with pytest.raises(ValueError, match="META_AGENT_RATELIMIT_BACKEND"):
        RateLimitConfig.from_env({"META_AGENT_RATELIMIT_BACKEND": "gcs"})


def test_rate_must_be_float() -> None:
    with pytest.raises(ValueError, match="META_AGENT_RATELIMIT_RATE_PER_SEC"):
        RateLimitConfig.from_env({"META_AGENT_RATELIMIT_RATE_PER_SEC": "fast"})


def test_rate_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        RateLimitConfig.from_env({"META_AGENT_RATELIMIT_RATE_PER_SEC": "0"})


def test_burst_must_be_int() -> None:
    with pytest.raises(ValueError, match="META_AGENT_RATELIMIT_BURST"):
        RateLimitConfig.from_env({"META_AGENT_RATELIMIT_BURST": "lots"})


def test_burst_must_be_at_least_one() -> None:
    with pytest.raises(ValueError, match="must be >= 1"):
        RateLimitConfig.from_env({"META_AGENT_RATELIMIT_BURST": "0"})


def test_factory_builds_noop() -> None:
    cfg = RateLimitConfig(backend="noop", rate_per_sec=1.0, burst=1, key_prefix="")
    limiter = build_rate_limiter_from_config(cfg)
    assert isinstance(limiter, NoopRateLimiter)


def test_factory_builds_in_memory() -> None:
    cfg = RateLimitConfig(backend="memory", rate_per_sec=3.0, burst=7, key_prefix="")
    limiter = build_rate_limiter_from_config(cfg)
    assert isinstance(limiter, InMemoryTokenBucketRateLimiter)


def test_factory_builds_redis_with_client() -> None:
    cfg = RateLimitConfig(backend="redis", rate_per_sec=5.0, burst=10, key_prefix="prod:")
    client = MagicMock()
    client.register_script = MagicMock(return_value=lambda **_kw: None)
    limiter = build_rate_limiter_from_config(cfg, redis_client=client)
    assert isinstance(limiter, RedisTokenBucketRateLimiter)
    client.register_script.assert_called_once()


def test_factory_redis_without_client_raises() -> None:
    cfg = RateLimitConfig(backend="redis", rate_per_sec=1.0, burst=1, key_prefix="")
    with pytest.raises(ValueError, match="requires a Redis client"):
        build_rate_limiter_from_config(cfg)
