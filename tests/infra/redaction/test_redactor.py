"""Unit tests for :class:`Redactor` + the built-in pattern library."""

from __future__ import annotations

import re

from meta_agent.infra.redaction.patterns import BUILTIN_PATTERNS, RedactionPattern
from meta_agent.infra.redaction.redactor import Redactor


def _make_redactor() -> Redactor:
    return Redactor()


def test_github_classic_token_redacted() -> None:
    text = "use ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa for auth"
    scrubbed, report = _make_redactor().scrub(text)
    assert "ghp_" not in scrubbed
    assert "[REDACTED:github_token]" in scrubbed
    assert report.hits == {"github_token": 1}


def test_openai_key_redacted() -> None:
    text = "set OPENAI_API_KEY=sk-proj-AAAAAAAAAAAAAAAAAAAAAA in env"
    scrubbed, report = _make_redactor().scrub(text)
    assert "sk-proj-" not in scrubbed
    assert report.hits == {"openai_key": 1}


def test_aws_access_key_redacted() -> None:
    text = "AKIAIOSFODNN7EXAMPLE plus ASIAIOSFODNN7EXAMPLE"
    scrubbed, report = _make_redactor().scrub(text)
    assert "AKIA" not in scrubbed
    assert "ASIA" not in scrubbed
    assert report.hits == {"aws_access_key_id": 2}


def test_jwt_redacted() -> None:
    text = (
        "Authorization=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTYiLCJuYW1lIjoiSm9obiJ9."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c next"
    )
    scrubbed, report = _make_redactor().scrub(text)
    assert "eyJ" not in scrubbed
    assert "jwt" in report.hits


def test_pem_private_key_block_redacted() -> None:
    text = (
        "key follows\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAo3jrnabu...\n"
        "...truncated...\n"
        "-----END RSA PRIVATE KEY-----\n"
        "after"
    )
    scrubbed, report = _make_redactor().scrub(text)
    assert "BEGIN RSA PRIVATE KEY" not in scrubbed
    assert "[REDACTED:private_key]" in scrubbed
    assert report.hits == {"private_key": 1}


def test_db_connection_uri_redacted() -> None:
    text = "DATABASE_URL=postgres://admin:hunter2@db.internal:5432/app and others"
    scrubbed, report = _make_redactor().scrub(text)
    assert "hunter2" not in scrubbed
    assert "admin" not in scrubbed
    assert report.hits == {"db_uri": 1}


def test_authorization_header_redacted() -> None:
    text = "curl -H 'Authorization: Bearer abc123xyzdef.tokenpart' https://api"
    scrubbed, report = _make_redactor().scrub(text)
    # The bearer envelope and the token inside are both consumed by
    # the authorization_header pattern in this case.
    assert "Bearer abc123" not in scrubbed
    assert "authorization_header" in report.hits


def test_email_redacted() -> None:
    text = "contact alice@example.com or bob.smith+tag@sub.example.co.uk"
    scrubbed, report = _make_redactor().scrub(text)
    assert "@example.com" not in scrubbed
    assert "@sub.example.co.uk" not in scrubbed
    assert report.hits == {"email": 2}


def test_multiple_distinct_secrets_in_one_pass() -> None:
    text = (
        "creds: ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "
        "and email me@example.org for sk-aaaaaaaaaaaaaaaaaaaaaaaa"
    )
    scrubbed, report = _make_redactor().scrub(text)
    assert "ghp_" not in scrubbed
    assert "sk-" not in scrubbed
    assert "@example.org" not in scrubbed
    assert report.hits == {
        "github_token": 1,
        "openai_key": 1,
        "email": 1,
    }
    assert report.total == 3
    assert report.any_redacted is True


def test_clean_text_passes_through_with_empty_report() -> None:
    text = "no secrets here, just a normal sentence about prime numbers"
    scrubbed, report = _make_redactor().scrub(text)
    assert scrubbed == text
    assert report.hits == {}
    assert report.any_redacted is False


def test_empty_and_none_inputs_handled() -> None:
    redactor = _make_redactor()
    assert redactor.scrub("") == ("", redactor.scrub("")[1])
    out, report = redactor.scrub(None)
    assert out == ""
    assert report.hits == {}


def test_non_string_input_coerced_via_str() -> None:
    out, report = _make_redactor().scrub(12345)
    assert out == "12345"
    assert report.hits == {}


def test_custom_extra_pattern_runs_after_builtins() -> None:
    """Caller-supplied patterns extend the library."""

    extra = RedactionPattern(
        label="internal_id",
        pattern=re.compile(r"\bINT-[0-9]{6}\b"),
    )
    redactor = Redactor((*BUILTIN_PATTERNS, extra))
    out, report = redactor.scrub("see INT-123456 for context")
    assert "INT-123456" not in out
    assert "[REDACTED:internal_id]" in out
    assert report.hits == {"internal_id": 1}


def test_scrub_str_helper_drops_report() -> None:
    out = _make_redactor().scrub_str("AKIAIOSFODNN7EXAMPLE")
    assert "AKIA" not in out


def test_placeholder_label_matches_pattern_label() -> None:
    for pattern in BUILTIN_PATTERNS:
        assert pattern.placeholder == f"[REDACTED:{pattern.label}]"


# ---------------------------------------------------------------------------
# False-positive guard tests — make sure benign text stays untouched.
# ---------------------------------------------------------------------------


def test_short_alphanumeric_not_redacted_as_token() -> None:
    text = "abc def ghp_short"  # ghp_short is too short for the GitHub pattern
    out, _ = _make_redactor().scrub(text)
    assert "ghp_short" in out


def test_http_url_without_credentials_not_redacted_as_db_uri() -> None:
    text = "see https://docs.example.com/path?q=1 for details"
    out, _ = _make_redactor().scrub(text)
    assert "https://docs.example.com/path" in out


def test_word_with_at_sign_but_not_email_not_redacted() -> None:
    text = "compile-time @decorator usage"
    out, _ = _make_redactor().scrub(text)
    assert "@decorator" in out
