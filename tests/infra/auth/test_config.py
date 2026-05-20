"""Unit tests for :mod:`meta_agent.infra.auth.config`."""

from __future__ import annotations

from typing import cast

import pytest

from meta_agent.infra.auth.config import (
    AuthConfig,
    build_token_validator_from_config,
)
from meta_agent.infra.auth.env_validator import EnvTokenValidator
from meta_agent.infra.auth.pg_validator import PgTokenValidator
from meta_agent.infra.persistence.pool import DatabasePool


def test_defaults_are_env_backend_with_empty_keys() -> None:
    cfg = AuthConfig.from_env({})
    assert cfg.backend == "env"
    assert cfg.api_keys == ""
    assert cfg.touch_last_used is True


def test_pg_backend_with_overrides() -> None:
    cfg = AuthConfig.from_env(
        {
            "META_AGENT_AUTH_BACKEND": "pg",
            "META_AGENT_AUTH_TOUCH_LAST_USED": "false",
        }
    )
    assert cfg.backend == "pg"
    assert cfg.touch_last_used is False


def test_env_backend_carries_api_keys() -> None:
    cfg = AuthConfig.from_env({"META_AGENT_API_KEYS": "tok:tenant:user"})
    assert cfg.backend == "env"
    assert cfg.api_keys == "tok:tenant:user"


@pytest.mark.parametrize(
    "env",
    [
        {"META_AGENT_AUTH_BACKEND": "ldap"},
        {"META_AGENT_AUTH_TOUCH_LAST_USED": "maybe"},
    ],
)
def test_invalid_env_raises(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        AuthConfig.from_env(env)


def test_factory_env_backend_builds_env_validator() -> None:
    cfg = AuthConfig(backend="env", api_keys="tok:t:u", touch_last_used=True)
    validator = build_token_validator_from_config(cfg)
    assert isinstance(validator, EnvTokenValidator)


def test_factory_pg_backend_requires_pool() -> None:
    cfg = AuthConfig(backend="pg", api_keys="", touch_last_used=True)
    with pytest.raises(ValueError, match="DatabasePool"):
        build_token_validator_from_config(cfg, pool=None)


def test_factory_pg_backend_builds_pg_validator() -> None:
    cfg = AuthConfig(backend="pg", api_keys="", touch_last_used=False)
    pool = cast(DatabasePool, object())  # never accessed in unit-test path
    validator = build_token_validator_from_config(cfg, pool=pool)
    assert isinstance(validator, PgTokenValidator)
