"""Unit tests for :class:`PromptAsset` and :func:`compute_content_hash`."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from meta_agent.core.domain.prompt_asset import PromptAsset, compute_content_hash


def _asset(**overrides: object) -> PromptAsset:
    base: dict[str, object] = {
        "prompt_id": "test.system",
        "version": 1,
        "content": "hello world",
        "created_at": datetime(2026, 5, 22, tzinfo=UTC),
    }
    base.update(overrides)
    return PromptAsset.model_validate(base)


def test_content_hash_is_lowercase_hex_sha256_of_content() -> None:
    asset = _asset(content="hello world")
    assert asset.content_hash == compute_content_hash("hello world")
    # known SHA-256("hello world") prefix
    assert asset.content_hash.startswith("b94d27b9934d3e08")
    assert asset.content_hash == asset.content_hash.lower()
    assert len(asset.content_hash) == 64


def test_compute_content_hash_uses_utf8() -> None:
    chinese = "你好"
    assert (
        compute_content_hash(chinese)
        == "670d9743542cae3ea7ebe36af56bd53648b0a1126162e78d81a32934a711302e"
    )


def test_prompt_asset_rejects_invalid_prompt_id() -> None:
    with pytest.raises(ValidationError):
        _asset(prompt_id="has space")
    with pytest.raises(ValidationError):
        _asset(prompt_id="")


def test_prompt_asset_rejects_version_below_one() -> None:
    with pytest.raises(ValidationError):
        _asset(version=0)


def test_prompt_asset_rejects_empty_content() -> None:
    with pytest.raises(ValidationError):
        _asset(content="")


def test_prompt_asset_is_frozen() -> None:
    asset = _asset()
    with pytest.raises(ValidationError):
        asset.content = "mutated"  # type: ignore[misc]


def test_tenant_id_optional_and_validated_when_present() -> None:
    assert _asset(tenant_id=None).tenant_id is None
    assert _asset(tenant_id="t-1").tenant_id == "t-1"
    with pytest.raises(ValidationError):
        _asset(tenant_id="")


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        PromptAsset.model_validate(
            {
                "prompt_id": "p",
                "version": 1,
                "content": "c",
                "created_at": datetime(2026, 5, 22, tzinfo=UTC),
                "bogus": "x",
            }
        )
