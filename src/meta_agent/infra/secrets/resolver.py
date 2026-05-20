"""Bridge :class:`Secrets` lookups back into the env-dict contract.

Existing config dataclasses (``OpenRouterConfig.from_env``,
``GitHubGitProviderConfig.from_env``, ``WorkerSettings.from_env``)
already accept a custom ``env`` dict so tests can run hermetically.
Rather than duplicate every ``from_env`` into a ``from_secrets``
variant, we resolve the well-known secret keys into a copy of the env
dict and let the existing factories consume the resolved view.

This keeps the diff to OpenRouter / GitHub configs at zero while still
threading the :class:`Secrets` port through the wiring code.
"""

from __future__ import annotations

import logging
import os
from typing import Final

from meta_agent.core.ports.secrets import (
    SECRET_KEY_GITHUB_TOKEN,
    SECRET_KEY_OPENROUTER_API_KEY,
    SecretBackendError,
    SecretNotFoundError,
    Secrets,
)

logger = logging.getLogger(__name__)

# ── Canonical secret-key → env-var override mapping ────────────────────────
#
# When a :class:`Secrets` backend resolves one of these keys, the value
# is written into the env dict under the corresponding name, overriding
# any existing value. Unmapped keys are returned by ``Secrets.get`` but
# not folded into the env (callers can still consume them directly).

SECRET_TO_ENV_NAME: Final[dict[str, str]] = {
    SECRET_KEY_OPENROUTER_API_KEY: "OPENROUTER_API_KEY",
    SECRET_KEY_GITHUB_TOKEN: "META_AGENT_GITHUB_TOKEN",
}


async def resolve_secret_env(
    secrets: Secrets,
    *,
    env: dict[str, str] | None = None,
    keys: tuple[str, ...] | None = None,
) -> dict[str, str]:
    """Return a copy of ``env`` with resolved secret values folded in.

    Parameters
    ----------
    secrets:
        The backend to consult.
    env:
        Base mapping to copy. Defaults to a snapshot of
        :mod:`os.environ`. Existing values are preserved unless the
        secret backend resolves the corresponding key, in which case
        the secret wins.
    keys:
        Secret keys to attempt to resolve. Defaults to every key in
        :data:`SECRET_TO_ENV_NAME`. Unknown keys (not in the mapping)
        are ignored with a debug log so callers can pass a superset
        without crashing.

    Behaviour
    ---------
    * :class:`SecretNotFoundError` → leave any existing env value
      unchanged. This is the expected path when a deployment uses the
      ``env`` backend and the env var already holds the credential.
    * :class:`SecretBackendError` → propagated so the caller (worker
      bootstrap / API lifespan) can decide whether to fail startup
      or continue with whatever env values exist.
    """
    base = dict(env if env is not None else os.environ)
    selected = keys if keys is not None else tuple(SECRET_TO_ENV_NAME.keys())
    for key in selected:
        env_name = SECRET_TO_ENV_NAME.get(key)
        if env_name is None:
            logger.debug("secrets.resolve_unknown_key key=%s", key)
            continue
        try:
            value = await secrets.get(key)
        except SecretNotFoundError:
            # Not configured in this backend; preserve any existing env
            # value silently — this is the dominant path for the env
            # backend where the env var is the source of truth.
            continue
        except SecretBackendError:
            # Surface backend faults so startup can decide; do not log
            # the key value even in trace mode.
            logger.warning("secrets.resolve_backend_error key=%s", key)
            raise
        base[env_name] = value
    return base


__all__ = ["SECRET_TO_ENV_NAME", "resolve_secret_env"]
