"""Unit tests for :class:`EnvTokenValidator`."""

from __future__ import annotations

import pytest

from meta_agent.infra.auth.env_validator import EnvTokenValidator


def test_empty_string_yields_zero_entries() -> None:
    validator = EnvTokenValidator("")
    assert validator.size == 0


async def test_unknown_token_returns_none() -> None:
    validator = EnvTokenValidator("tok-a:tenant-1:user-1")
    assert await validator.validate("nope") is None


async def test_empty_token_returns_none() -> None:
    validator = EnvTokenValidator("tok-a:tenant-1:user-1")
    assert await validator.validate("") is None


async def test_match_resolves_to_principal() -> None:
    validator = EnvTokenValidator("tok-a:tenant-1:user-1")
    principal = await validator.validate("tok-a")
    assert principal is not None
    assert principal.tenant_id == "tenant-1"
    assert principal.principal_id == "user-1"
    assert principal.scopes == ()


async def test_scopes_parsed_with_semicolon_separator() -> None:
    validator = EnvTokenValidator("tok-a:tenant-1:user-1:read;write")
    principal = await validator.validate("tok-a")
    assert principal is not None
    assert principal.scopes == ("read", "write")


async def test_multiple_entries_each_resolvable() -> None:
    raw = " tok-a:tenant-1:user-1 , tok-b:tenant-2:user-2:admin "
    validator = EnvTokenValidator(raw)
    assert validator.size == 2
    pa = await validator.validate("tok-a")
    pb = await validator.validate("tok-b")
    assert pa is not None and pa.tenant_id == "tenant-1"
    assert pb is not None and pb.tenant_id == "tenant-2"
    assert pb.scopes == ("admin",)


def test_blank_entries_skipped_between_commas() -> None:
    validator = EnvTokenValidator("tok-a:tenant-1:user-1,,tok-b:tenant-2:user-2")
    assert validator.size == 2


@pytest.mark.parametrize(
    "raw",
    [
        "missing-fields",
        "tok:only-two",
        "tok:tenant:user:scopes:extra",
        ":tenant:user",
        "tok::user",
        "tok:tenant:",
    ],
)
def test_malformed_entry_raises(raw: str) -> None:
    with pytest.raises(ValueError, match="META_AGENT_API_KEYS"):
        EnvTokenValidator(raw)
