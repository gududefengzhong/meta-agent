"""Built-in redaction patterns for the Phase γ-D scrubber.

Each pattern is a compiled regex plus a stable placeholder string.
The scanner replaces *every* match with ``[REDACTED:<label>]`` so
the redacted output stays human-readable and the LLM can still see
"there was a secret here" without seeing the secret itself.

Patterns are sorted from most specific to least specific in the
matching loop (the order in :data:`BUILTIN_PATTERNS`) so an
``Authorization: Bearer ghp_...`` does not get partially redacted by
the header pattern before the GitHub-token pattern can claim the
inner secret. See :class:`Redactor` for the dispatch mechanics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RedactionPattern:
    """One named regex with a stable placeholder label.

    ``label`` ends up inside the placeholder string as
    ``[REDACTED:<label>]``; it is stable across releases so audit log
    consumers can grep for specific redaction kinds without parsing
    the original secret.
    """

    label: str
    pattern: re.Pattern[str]

    @property
    def placeholder(self) -> str:
        return f"[REDACTED:{self.label}]"


# ---------------------------------------------------------------------------
# Compiled regex library.
#
# Patterns are written defensively: anchored with character class
# boundaries instead of ``\b`` (which mis-classifies hyphens and
# slashes), and bounded by reasonable maximum lengths so a pathological
# input cannot wedge the scanner on a single token.
# ---------------------------------------------------------------------------


_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY-----"
    r"[\s\S]{1,4096}?-----END (?:RSA |EC |DSA |OPENSSH |ENCRYPTED |PGP )?PRIVATE KEY-----"
)

_GITHUB_TOKEN = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")

_OPENAI_KEY = re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")

# AWS access key ids are 20 chars all uppercase + digits, prefixed by
# AKIA (long-term) or ASIA (session). Secret keys are 40-char base64-ish
# and impossible to distinguish from random text without a paired
# anchor; we redact the more-distinct id only.
_AWS_ACCESS_KEY_ID = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")

_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")

# Authorization: Bearer <token> / Basic <base64>. Greedy on the value
# side but bounded so a stray newline ends the redaction window.
_AUTHORIZATION_HEADER = re.compile(
    r"Authorization:\s*(?:Bearer|Basic)\s+[A-Za-z0-9._\-+=/]{8,}",
    re.IGNORECASE,
)

# DB connection URIs with embedded credentials: postgres://user:pass@host
# Captures the whole URI so logs / prompts don't accidentally leak the
# host either. Schemes are explicit (no general ``://`` capture which
# would catch http:// URIs that are not necessarily secrets).
_DB_CONNECTION_URI = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)s?://"
    r"[^\s:@/]+:[^\s@/]+@[^\s/]+(?:/[^\s]*)?",
    re.IGNORECASE,
)

# Email addresses. PII not security per se, but operators usually want
# them masked in audit / prompt traces.
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,253}\.[A-Za-z]{2,24}\b")


BUILTIN_PATTERNS: tuple[RedactionPattern, ...] = (
    # Order matters: PEM blocks first (so a key embedded in a longer
    # string takes precedence over inner regex hits), then provider-
    # specific token formats, then envelope-style patterns, finally
    # broad PII heuristics.
    RedactionPattern(label="private_key", pattern=_PEM_PRIVATE_KEY),
    RedactionPattern(label="github_token", pattern=_GITHUB_TOKEN),
    RedactionPattern(label="openai_key", pattern=_OPENAI_KEY),
    RedactionPattern(label="aws_access_key_id", pattern=_AWS_ACCESS_KEY_ID),
    RedactionPattern(label="jwt", pattern=_JWT),
    RedactionPattern(label="db_uri", pattern=_DB_CONNECTION_URI),
    RedactionPattern(label="authorization_header", pattern=_AUTHORIZATION_HEADER),
    RedactionPattern(label="email", pattern=_EMAIL),
)
"""Default pattern set applied to every text the scrubber sees.

Operators that need extra patterns construct a :class:`Redactor`
with ``patterns=BUILTIN_PATTERNS + (extra_pattern,)``; the scanner
treats the list as ordered and applies each in turn.
"""
