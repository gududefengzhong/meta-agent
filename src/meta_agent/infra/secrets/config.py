"""Env-driven configuration + factory for :class:`Secrets`.

Picks one of:

* ``env``  — :class:`EnvSecrets` (default; reads from process env)
* ``file`` — :class:`FileSecrets` (reads JSON from ``META_AGENT_SECRETS_FILE``)

Env variables
=============

============================== ====================================
``META_AGENT_SECRETS_BACKEND`` ``env`` / ``file`` (default ``env``)
``META_AGENT_SECRETS_FILE``    JSON path, required when backend=file
============================== ====================================

The default ``env`` is a zero-behaviour-change wrapper: existing
deployments that set ``OPENROUTER_API_KEY`` / ``META_AGENT_GITHUB_TOKEN``
keep working without further config. ``file`` is the opt-in path for
Kubernetes secret-volume mounts and local dev sandboxes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final, Literal

from meta_agent.core.ports.secrets import Secrets
from meta_agent.infra.secrets.env import EnvSecrets
from meta_agent.infra.secrets.file import FileSecrets

_BACKEND_ENV: Final[str] = "META_AGENT_SECRETS_BACKEND"
_FILE_ENV: Final[str] = "META_AGENT_SECRETS_FILE"

_DEFAULT_BACKEND: Final[str] = "env"

SecretsBackend = Literal["env", "file"]
_SUPPORTED_BACKENDS: Final[tuple[SecretsBackend, ...]] = ("env", "file")


@dataclass(frozen=True, slots=True)
class SecretsConfig:
    """Parsed env settings for the secrets factory."""

    backend: SecretsBackend
    file_path: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> SecretsConfig:
        source: dict[str, str] = dict(env if env is not None else os.environ)
        backend_raw = source.get(_BACKEND_ENV, _DEFAULT_BACKEND).strip().lower()
        if backend_raw not in _SUPPORTED_BACKENDS:
            raise ValueError(f"{_BACKEND_ENV}={backend_raw!r} not in {_SUPPORTED_BACKENDS}")
        file_path = source.get(_FILE_ENV, "").strip()
        if backend_raw == "file" and not file_path:
            raise ValueError(f"{_BACKEND_ENV}=file requires {_FILE_ENV} to be set")
        return cls(backend=backend_raw, file_path=file_path)


def build_secrets_from_config(
    config: SecretsConfig,
    *,
    env: dict[str, str] | None = None,
) -> Secrets:
    """Materialise a :class:`Secrets` from a parsed config.

    Parameters
    ----------
    config:
        Result of :meth:`SecretsConfig.from_env`.
    env:
        Captured environment used by the ``env`` backend. Defaults to
        a snapshot of :mod:`os.environ`; tests should pass a frozen
        dict to keep the factory hermetic.
    """
    if config.backend == "env":
        env_source = env if env is not None else dict(os.environ)
        return EnvSecrets(env_source)
    if config.backend == "file":
        return FileSecrets.from_path(config.file_path)
    raise AssertionError(f"unhandled secrets backend: {config.backend!r}")


def build_secrets_from_env(env: dict[str, str] | None = None) -> Secrets:
    """Read env once, materialise a :class:`Secrets`."""
    return build_secrets_from_config(SecretsConfig.from_env(env), env=env)


__all__ = [
    "SecretsBackend",
    "SecretsConfig",
    "build_secrets_from_config",
    "build_secrets_from_env",
]
