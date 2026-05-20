"""Unit tests for :class:`FileSecrets`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meta_agent.core.ports.secrets import (
    SECRET_KEY_OPENROUTER_API_KEY,
    SecretBackendError,
    SecretNotFoundError,
)
from meta_agent.infra.secrets.file import FileSecrets


async def test_load_resolves_keys(tmp_path: Path) -> None:
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text(
        json.dumps({SECRET_KEY_OPENROUTER_API_KEY: "sk-or-test"}),
        encoding="utf-8",
    )
    secrets = FileSecrets.from_path(secrets_file)
    assert await secrets.get(SECRET_KEY_OPENROUTER_API_KEY) == "sk-or-test"


async def test_missing_key_raises_not_found(tmp_path: Path) -> None:
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text("{}", encoding="utf-8")
    secrets = FileSecrets.from_path(secrets_file)
    with pytest.raises(SecretNotFoundError, match="not present"):
        await secrets.get(SECRET_KEY_OPENROUTER_API_KEY)


async def test_empty_value_raises_not_found(tmp_path: Path) -> None:
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text(
        json.dumps({SECRET_KEY_OPENROUTER_API_KEY: "   "}),
        encoding="utf-8",
    )
    secrets = FileSecrets.from_path(secrets_file)
    with pytest.raises(SecretNotFoundError, match="present but empty"):
        await secrets.get(SECRET_KEY_OPENROUTER_API_KEY)


def test_missing_file_raises_backend_error(tmp_path: Path) -> None:
    with pytest.raises(SecretBackendError, match="unable to read"):
        FileSecrets.from_path(tmp_path / "missing.json")


def test_invalid_json_raises_backend_error(tmp_path: Path) -> None:
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text("not-json", encoding="utf-8")
    with pytest.raises(SecretBackendError, match="not valid JSON"):
        FileSecrets.from_path(secrets_file)


def test_non_object_root_raises_backend_error(tmp_path: Path) -> None:
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text("[]", encoding="utf-8")
    with pytest.raises(SecretBackendError, match="JSON object"):
        FileSecrets.from_path(secrets_file)


def test_non_string_value_raises_backend_error(tmp_path: Path) -> None:
    secrets_file = tmp_path / "secrets.json"
    secrets_file.write_text(json.dumps({"k": 42}), encoding="utf-8")
    with pytest.raises(SecretBackendError, match="non-string value"):
        FileSecrets.from_path(secrets_file)
