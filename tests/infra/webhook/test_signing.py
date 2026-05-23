"""Unit tests for the HMAC SHA-256 signing helper."""

from __future__ import annotations

import pytest

from meta_agent.infra.webhook.signing import (
    SIGNATURE_HEADER,
    SIGNATURE_PREFIX,
    compute_signature,
    verify_signature,
)


def test_compute_signature_known_vector() -> None:
    # Reference vector: known HMAC-SHA256(key="secret", msg=b"hello").
    sig = compute_signature("secret", b"hello")
    assert sig.startswith(SIGNATURE_PREFIX)
    assert sig == ("sha256=88aab3ede8d3adf94d26ab90d3bafd4a2083070c3bcce9c014ee04a443847c0b")


def test_compute_signature_changes_with_secret() -> None:
    body = b"the body bytes"
    assert compute_signature("secret-1", body) != compute_signature("secret-2", body)


def test_compute_signature_changes_with_body() -> None:
    secret = "shared-secret"
    assert compute_signature(secret, b"a") != compute_signature(secret, b"b")


def test_compute_signature_rejects_empty_secret() -> None:
    with pytest.raises(ValueError, match="secret"):
        compute_signature("", b"body")


def test_verify_signature_round_trip() -> None:
    body = b'{"hello": "world"}'
    sig = compute_signature("k", body)
    assert verify_signature("k", body, sig) is True


def test_verify_signature_fails_on_tamper() -> None:
    body = b'{"hello": "world"}'
    sig = compute_signature("k", body)
    assert verify_signature("k", b'{"hello": "evil"}', sig) is False


def test_verify_signature_fails_on_wrong_secret() -> None:
    body = b'{"x": 1}'
    sig = compute_signature("right", body)
    assert verify_signature("wrong", body, sig) is False


def test_signature_header_constant_matches_github_scheme() -> None:
    assert SIGNATURE_HEADER == "X-Meta-Agent-Signature"
    assert SIGNATURE_PREFIX == "sha256="
