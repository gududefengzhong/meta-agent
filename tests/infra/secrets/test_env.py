"""Unit tests for :class:`EnvSecrets`."""

from __future__ import annotations

import pytest

from meta_agent.core.ports.secrets import (
    SECRET_KEY_GITHUB_TOKEN,
    SECRET_KEY_OPENROUTER_API_KEY,
    SecretNotFoundError,
)
from meta_agent.infra.secrets.env import EnvSecrets


async def test_known_keys_resolve_to_env_values() -> None:
    secrets = EnvSecrets(
        {
            "OPENROUTER_API_KEY": "sk-or-test",
            "META_AGENT_GITHUB_TOKEN": "ghp-test",
        }
    )
    assert await secrets.get(SECRET_KEY_OPENROUTER_API_KEY) == "sk-or-test"
    assert await secrets.get(SECRET_KEY_GITHUB_TOKEN) == "ghp-test"


async def test_unset_env_var_raises_not_found() -> None:
    secrets = EnvSecrets({})
    with pytest.raises(SecretNotFoundError, match="unset or empty"):
        await secrets.get(SECRET_KEY_OPENROUTER_API_KEY)


async def test_empty_env_var_raises_not_found() -> None:
    secrets = EnvSecrets({"OPENROUTER_API_KEY": "   "})
    with pytest.raises(SecretNotFoundError, match="unset or empty"):
        await secrets.get(SECRET_KEY_OPENROUTER_API_KEY)


async def test_unknown_secret_key_raises_not_found() -> None:
    secrets = EnvSecrets({"OPENROUTER_API_KEY": "x"})
    with pytest.raises(SecretNotFoundError, match="no env var configured"):
        await secrets.get("unmapped.key")


async def test_value_is_stripped() -> None:
    secrets = EnvSecrets({"OPENROUTER_API_KEY": "  sk-or-test  "})
    assert await secrets.get(SECRET_KEY_OPENROUTER_API_KEY) == "sk-or-test"


async def test_custom_key_map_used_when_provided() -> None:
    secrets = EnvSecrets(
        {"CUSTOM_KEY": "value-1"},
        key_to_env_name={"custom.key": "CUSTOM_KEY"},
    )
    assert await secrets.get("custom.key") == "value-1"
    # Default keys disappear under a custom map
    with pytest.raises(SecretNotFoundError, match="no env var configured"):
        await secrets.get(SECRET_KEY_OPENROUTER_API_KEY)
