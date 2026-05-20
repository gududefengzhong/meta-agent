"""Environment-variable-backed :class:`Secrets` adapter.

Each well-known secret key maps to a single env var. Empty / missing
env vars raise :class:`SecretNotFoundError` so callers can distinguish
"key not configured" from "key configured to empty string" — the
latter would be a deployment bug we want to surface, not silently
swallow.
"""

from __future__ import annotations

import os
from typing import Final

from meta_agent.core.ports.secrets import (
    SECRET_KEY_GITHUB_TOKEN,
    SECRET_KEY_OPENROUTER_API_KEY,
    SecretNotFoundError,
    Secrets,
)

# ── Canonical secret-key → env-var mapping ──────────────────────────────────
#
# A single source of truth used by both :class:`EnvSecrets` and
# :func:`resolve_secret_env`. Keep in sync with ``KNOWN_SECRET_KEYS``.

DEFAULT_KEY_TO_ENV_NAME: Final[dict[str, str]] = {
    SECRET_KEY_OPENROUTER_API_KEY: "OPENROUTER_API_KEY",
    SECRET_KEY_GITHUB_TOKEN: "META_AGENT_GITHUB_TOKEN",
}


class EnvSecrets(Secrets):
    """Read secrets straight from a captured env mapping.

    The mapping is captured at construction so tests can supply a
    deterministic env without monkey-patching :mod:`os.environ`. In
    production the default :meth:`from_environ` snapshot of process env
    is used.
    """

    def __init__(
        self,
        env: dict[str, str],
        *,
        key_to_env_name: dict[str, str] | None = None,
    ) -> None:
        self._env = dict(env)
        self._key_to_env_name = dict(key_to_env_name or DEFAULT_KEY_TO_ENV_NAME)

    @classmethod
    def from_environ(
        cls,
        *,
        key_to_env_name: dict[str, str] | None = None,
    ) -> EnvSecrets:
        """Snapshot :mod:`os.environ` at construction time."""
        return cls(dict(os.environ), key_to_env_name=key_to_env_name)

    async def get(self, key: str) -> str:
        env_name = self._key_to_env_name.get(key)
        if env_name is None:
            raise SecretNotFoundError(f"no env var configured for secret key {key!r}")
        value = self._env.get(env_name, "").strip()
        if not value:
            raise SecretNotFoundError(f"secret key {key!r} (env var {env_name}) is unset or empty")
        return value


__all__ = ["DEFAULT_KEY_TO_ENV_NAME", "EnvSecrets"]
