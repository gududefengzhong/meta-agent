"""Unit tests for :mod:`meta_agent.infra.secrets.config`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meta_agent.infra.secrets.config import (
    SecretsConfig,
    build_secrets_from_config,
    build_secrets_from_env,
)
from meta_agent.infra.secrets.env import EnvSecrets
from meta_agent.infra.secrets.file import FileSecrets


def test_defaults_to_env_backend() -> None:
    cfg = SecretsConfig.from_env({})
    assert cfg.backend == "env"
    assert cfg.file_path == ""


def test_file_backend_requires_path() -> None:
    with pytest.raises(ValueError, match="META_AGENT_SECRETS_FILE"):
        SecretsConfig.from_env({"META_AGENT_SECRETS_BACKEND": "file"})


def test_file_backend_accepts_path() -> None:
    cfg = SecretsConfig.from_env(
        {
            "META_AGENT_SECRETS_BACKEND": "file",
            "META_AGENT_SECRETS_FILE": "/etc/agent.json",
        }
    )
    assert cfg.backend == "file"
    assert cfg.file_path == "/etc/agent.json"


def test_invalid_backend_raises() -> None:
    with pytest.raises(ValueError, match="META_AGENT_SECRETS_BACKEND"):
        SecretsConfig.from_env({"META_AGENT_SECRETS_BACKEND": "vault"})


def test_factory_env_backend_builds_env_secrets() -> None:
    cfg = SecretsConfig(backend="env", file_path="")
    secrets = build_secrets_from_config(cfg, env={"OPENROUTER_API_KEY": "x"})
    assert isinstance(secrets, EnvSecrets)


def test_factory_file_backend_builds_file_secrets(tmp_path: Path) -> None:
    secrets_file = tmp_path / "s.json"
    secrets_file.write_text(json.dumps({"openrouter.api_key": "x"}), encoding="utf-8")
    cfg = SecretsConfig(backend="file", file_path=str(secrets_file))
    secrets = build_secrets_from_config(cfg)
    assert isinstance(secrets, FileSecrets)


def test_build_from_env_helper_chains_config_and_factory() -> None:
    secrets = build_secrets_from_env({})
    assert isinstance(secrets, EnvSecrets)
