"""Env-parsing + factory tests for :mod:`meta_agent.infra.circuitbreaker.config`."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from meta_agent.infra.circuitbreaker.config import (
    CircuitBreakerConfig,
    build_circuit_breaker_from_config,
)
from meta_agent.infra.circuitbreaker.in_memory import InMemoryCircuitBreaker
from meta_agent.infra.circuitbreaker.noop import NoopCircuitBreaker
from meta_agent.infra.circuitbreaker.redis_breaker import RedisCircuitBreaker


def test_defaults_select_noop_with_documented_numbers() -> None:
    cfg = CircuitBreakerConfig.from_env({})
    assert cfg.backend == "noop"
    assert cfg.failure_threshold == 5
    assert cfg.window_seconds == 30.0
    assert cfg.cooldown_seconds == 30.0
    assert cfg.key_prefix == ""


def test_explicit_backend_overrides() -> None:
    cfg = CircuitBreakerConfig.from_env(
        {
            "META_AGENT_CIRCUITBREAKER_BACKEND": "memory",
            "META_AGENT_CIRCUITBREAKER_FAILURE_THRESHOLD": "3",
            "META_AGENT_CIRCUITBREAKER_WINDOW_SECONDS": "15.0",
            "META_AGENT_CIRCUITBREAKER_COOLDOWN_SECONDS": "60.0",
            "META_AGENT_CIRCUITBREAKER_KEY_PREFIX": "stage:",
        }
    )
    assert cfg.backend == "memory"
    assert cfg.failure_threshold == 3
    assert cfg.window_seconds == 15.0
    assert cfg.cooldown_seconds == 60.0
    assert cfg.key_prefix == "stage:"


def test_backend_case_and_whitespace_insensitive() -> None:
    cfg = CircuitBreakerConfig.from_env({"META_AGENT_CIRCUITBREAKER_BACKEND": "  Redis  "})
    assert cfg.backend == "redis"


def test_unknown_backend_rejected() -> None:
    with pytest.raises(ValueError, match="META_AGENT_CIRCUITBREAKER_BACKEND"):
        CircuitBreakerConfig.from_env({"META_AGENT_CIRCUITBREAKER_BACKEND": "fuse"})


def test_threshold_must_be_int() -> None:
    with pytest.raises(ValueError, match="META_AGENT_CIRCUITBREAKER_FAILURE_THRESHOLD"):
        CircuitBreakerConfig.from_env({"META_AGENT_CIRCUITBREAKER_FAILURE_THRESHOLD": "many"})


def test_threshold_must_be_at_least_one() -> None:
    with pytest.raises(ValueError, match="must be >= 1"):
        CircuitBreakerConfig.from_env({"META_AGENT_CIRCUITBREAKER_FAILURE_THRESHOLD": "0"})


def test_window_must_be_float() -> None:
    with pytest.raises(ValueError, match="META_AGENT_CIRCUITBREAKER_WINDOW_SECONDS"):
        CircuitBreakerConfig.from_env({"META_AGENT_CIRCUITBREAKER_WINDOW_SECONDS": "long"})


def test_window_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        CircuitBreakerConfig.from_env({"META_AGENT_CIRCUITBREAKER_WINDOW_SECONDS": "0"})


def test_cooldown_must_be_float() -> None:
    with pytest.raises(ValueError, match="META_AGENT_CIRCUITBREAKER_COOLDOWN_SECONDS"):
        CircuitBreakerConfig.from_env({"META_AGENT_CIRCUITBREAKER_COOLDOWN_SECONDS": "soon"})


def test_cooldown_must_be_positive() -> None:
    with pytest.raises(ValueError, match="must be > 0"):
        CircuitBreakerConfig.from_env({"META_AGENT_CIRCUITBREAKER_COOLDOWN_SECONDS": "-1"})


def test_factory_builds_noop() -> None:
    cfg = CircuitBreakerConfig(
        backend="noop",
        failure_threshold=1,
        window_seconds=1.0,
        cooldown_seconds=1.0,
        key_prefix="",
    )
    breaker = build_circuit_breaker_from_config(cfg)
    assert isinstance(breaker, NoopCircuitBreaker)


def test_factory_builds_in_memory() -> None:
    cfg = CircuitBreakerConfig(
        backend="memory",
        failure_threshold=3,
        window_seconds=10.0,
        cooldown_seconds=15.0,
        key_prefix="",
    )
    breaker = build_circuit_breaker_from_config(cfg)
    assert isinstance(breaker, InMemoryCircuitBreaker)


def test_factory_builds_redis_with_client() -> None:
    cfg = CircuitBreakerConfig(
        backend="redis",
        failure_threshold=5,
        window_seconds=30.0,
        cooldown_seconds=30.0,
        key_prefix="prod:",
    )
    client = MagicMock()
    client.register_script = MagicMock(return_value=lambda **_kw: None)
    breaker = build_circuit_breaker_from_config(cfg, redis_client=client)
    assert isinstance(breaker, RedisCircuitBreaker)
    # Gate + record scripts both registered.
    assert client.register_script.call_count == 2


def test_factory_redis_without_client_raises() -> None:
    cfg = CircuitBreakerConfig(
        backend="redis",
        failure_threshold=1,
        window_seconds=1.0,
        cooldown_seconds=1.0,
        key_prefix="",
    )
    with pytest.raises(ValueError, match="requires a Redis client"):
        build_circuit_breaker_from_config(cfg)
