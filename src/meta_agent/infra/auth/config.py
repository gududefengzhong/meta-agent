"""Env-driven configuration + factory for :class:`TokenValidator`.

Picks one of:

* ``env``  — :class:`EnvTokenValidator` (default; dev / CI; CSV in env var)
* ``pg``   — :class:`PgTokenValidator`   (production; ``api_keys`` table)

Env variables
=============

================================= ===========================================
``META_AGENT_AUTH_BACKEND``       ``env`` / ``pg`` (default ``env``)
``META_AGENT_API_KEYS``           CSV: ``token:tenant:principal[:scopes]``
``META_AGENT_AUTH_TOUCH_LAST_USED`` ``true`` / ``false`` (default ``true``)
================================= ===========================================

``env`` is the deliberate default so an instance with no auth env vars
set rejects every request (empty CSV → empty table → every token
unknown). Operators must opt in by either populating ``META_AGENT_API_KEYS``
or switching to ``pg`` and provisioning the table.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final, Literal

from meta_agent.core.ports.auth import TokenValidator
from meta_agent.infra.auth.env_validator import EnvTokenValidator
from meta_agent.infra.auth.pg_validator import PgTokenValidator
from meta_agent.infra.persistence.pool import DatabasePool

_BACKEND_ENV: Final[str] = "META_AGENT_AUTH_BACKEND"
_API_KEYS_ENV: Final[str] = "META_AGENT_API_KEYS"
_TOUCH_ENV: Final[str] = "META_AGENT_AUTH_TOUCH_LAST_USED"

_DEFAULT_BACKEND: Final[str] = "env"
_DEFAULT_TOUCH: Final[str] = "true"

AuthBackend = Literal["env", "pg"]
_SUPPORTED_BACKENDS: Final[tuple[AuthBackend, ...]] = ("env", "pg")


def _parse_bool(raw: str, *, env_name: str) -> bool:
    value = raw.strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return True
    if value in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{env_name}={raw!r} is not a boolean")


@dataclass(frozen=True, slots=True)
class AuthConfig:
    """Parsed env settings for the token-validator factory."""

    backend: AuthBackend
    api_keys: str
    touch_last_used: bool

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AuthConfig:
        source: dict[str, str] = dict(env if env is not None else os.environ)
        backend_raw = source.get(_BACKEND_ENV, _DEFAULT_BACKEND).strip().lower()
        if backend_raw not in _SUPPORTED_BACKENDS:
            raise ValueError(f"{_BACKEND_ENV}={backend_raw!r} not in {_SUPPORTED_BACKENDS}")
        touch = _parse_bool(source.get(_TOUCH_ENV, _DEFAULT_TOUCH), env_name=_TOUCH_ENV)
        return cls(
            backend=backend_raw,
            api_keys=source.get(_API_KEYS_ENV, ""),
            touch_last_used=touch,
        )


def build_token_validator_from_config(
    config: AuthConfig,
    *,
    pool: DatabasePool | None = None,
) -> TokenValidator:
    """Materialise a :class:`TokenValidator` from a parsed config.

    Parameters
    ----------
    config:
        Result of :meth:`AuthConfig.from_env`.
    pool:
        Required when ``config.backend == "pg"`` so the validator can
        query ``api_keys``; ignored for the env backend.

    Raises
    ------
    ValueError
        If ``backend == "pg"`` but no pool was provided.
    """
    if config.backend == "env":
        return EnvTokenValidator(config.api_keys)
    if config.backend == "pg":
        if pool is None:
            raise ValueError(
                f"{_BACKEND_ENV}=pg requires a DatabasePool to be passed to the factory"
            )
        return PgTokenValidator(pool, touch_last_used=config.touch_last_used)
    raise AssertionError(f"unhandled auth backend: {config.backend!r}")


def build_token_validator_from_env(
    env: dict[str, str] | None = None,
    *,
    pool: DatabasePool | None = None,
) -> TokenValidator:
    """Read env once, materialise a :class:`TokenValidator`."""
    return build_token_validator_from_config(AuthConfig.from_env(env), pool=pool)


__all__ = [
    "AuthBackend",
    "AuthConfig",
    "build_token_validator_from_config",
    "build_token_validator_from_env",
]
