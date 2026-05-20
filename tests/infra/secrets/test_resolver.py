"""Unit tests for :func:`resolve_secret_env`."""

from __future__ import annotations

import pytest

from meta_agent.core.ports.secrets import (
    SECRET_KEY_GITHUB_TOKEN,
    SECRET_KEY_OPENROUTER_API_KEY,
    SecretBackendError,
    SecretNotFoundError,
    Secrets,
)
from meta_agent.infra.secrets.resolver import (
    SECRET_TO_ENV_NAME,
    resolve_secret_env,
)


class _StubSecrets(Secrets):
    def __init__(self, mapping: dict[str, str | BaseException]) -> None:
        self._mapping = mapping

    async def get(self, key: str) -> str:
        value = self._mapping.get(key)
        if value is None:
            raise SecretNotFoundError(key)
        if isinstance(value, BaseException):
            raise value
        return value


async def test_resolved_secrets_overwrite_existing_env() -> None:
    secrets = _StubSecrets(
        {
            SECRET_KEY_OPENROUTER_API_KEY: "sk-or-new",
            SECRET_KEY_GITHUB_TOKEN: "ghp-new",
        }
    )
    resolved = await resolve_secret_env(
        secrets,
        env={
            "OPENROUTER_API_KEY": "sk-or-old",
            "META_AGENT_GITHUB_TOKEN": "ghp-old",
            "UNRELATED": "keep",
        },
    )
    assert resolved["OPENROUTER_API_KEY"] == "sk-or-new"
    assert resolved["META_AGENT_GITHUB_TOKEN"] == "ghp-new"
    assert resolved["UNRELATED"] == "keep"


async def test_not_found_preserves_existing_env() -> None:
    secrets = _StubSecrets({})
    resolved = await resolve_secret_env(
        secrets,
        env={"OPENROUTER_API_KEY": "sk-or-existing"},
    )
    assert resolved["OPENROUTER_API_KEY"] == "sk-or-existing"


async def test_backend_error_propagates() -> None:
    secrets = _StubSecrets({SECRET_KEY_OPENROUTER_API_KEY: SecretBackendError("kms down")})
    with pytest.raises(SecretBackendError, match="kms down"):
        await resolve_secret_env(secrets, env={})


async def test_unknown_key_in_selection_skipped() -> None:
    secrets = _StubSecrets({})
    # ``"unmapped.key"`` is not in SECRET_TO_ENV_NAME; resolver must
    # ignore it instead of asking the backend.
    resolved = await resolve_secret_env(
        secrets,
        env={"KEEP": "1"},
        keys=("unmapped.key",),
    )
    assert resolved == {"KEEP": "1"}


async def test_default_keys_cover_well_known_set() -> None:
    # The mapping is the contract; tests treat it as a sanity check
    # so a future addition cannot silently drop OpenRouter / GitHub.
    assert SECRET_KEY_OPENROUTER_API_KEY in SECRET_TO_ENV_NAME
    assert SECRET_KEY_GITHUB_TOKEN in SECRET_TO_ENV_NAME


async def test_returned_dict_is_a_copy() -> None:
    secrets = _StubSecrets({})
    base = {"KEEP": "1"}
    resolved = await resolve_secret_env(secrets, env=base)
    resolved["KEEP"] = "mutated"
    assert base["KEEP"] == "1"
