"""HMAC SHA-256 signing for outbound webhook payloads.

The signature scheme is intentionally GitHub-compatible: each request
carries ``X-Meta-Agent-Signature: sha256=<hex>``. Subscribers verify by
recomputing the same HMAC over the raw request body with their shared
secret and comparing in constant time.

The helper is centralised here so the dispatcher and any future
verifier ports (e.g. the inbound webhook receiver in a much later
phase) agree on the wire format. No third-party dependency: stdlib
``hmac`` is sufficient and avoids a supply-chain hop for a critical
path.
"""

from __future__ import annotations

import hashlib
import hmac

SIGNATURE_HEADER = "X-Meta-Agent-Signature"
"""HTTP header name carrying the signature."""

SIGNATURE_PREFIX = "sha256="
"""Value prefix preceding the hex digest. Matches GitHub's scheme."""


def compute_signature(secret: str, body: bytes) -> str:
    """Return ``sha256=<hex>`` for ``body`` signed under ``secret``.

    ``secret`` is encoded as UTF-8; subscribers MUST agree on that
    encoding (callers that need a binary secret should pre-hex it on
    both sides). ``body`` is the raw bytes that will be sent over the
    wire — JSON encoding happens before signing, never after, so the
    subscriber's verification reproduces the exact bytes.
    """

    if not secret:
        raise ValueError("compute_signature: secret must be a non-empty string")
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify_signature(secret: str, body: bytes, signature: str) -> bool:
    """Constant-time verification helper.

    Returns ``True`` iff ``signature`` exactly matches the freshly
    computed signature for ``body`` under ``secret``. Mismatched
    prefixes, hex case, or whitespace all fail. The comparison uses
    :func:`hmac.compare_digest` so timing leaks do not aid attackers.
    """

    expected = compute_signature(secret, body)
    return hmac.compare_digest(expected.encode("ascii"), signature.encode("ascii"))
