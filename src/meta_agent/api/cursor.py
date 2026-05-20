"""Opaque keyset-cursor codec for query API pagination.

A cursor encodes the ``(timestamp, id)`` tuple of the last row of the
previous page. Clients pass the returned ``next_cursor`` back verbatim;
the server decodes it into a typed tuple that the repository ports
consume as ``before``. The encoding is intentionally not signed — the
cursor only references rows the caller is already authorised to see
(tenant isolation is enforced separately by the auth port and the
``check_tenant`` guard); cryptographic binding would be overkill here.

Format
------
``base64url(<iso8601_utc_timestamp>|<id>)`` with no padding. The
timestamp is rendered with microsecond precision and an explicit
``+00:00`` suffix so round-tripping through the codec is lossless.
"""

from __future__ import annotations

import base64
import binascii
from datetime import datetime


class CursorError(ValueError):
    """Raised when a client-supplied cursor cannot be decoded.

    Subclasses :class:`ValueError` so FastAPI handlers can surface 400
    on the public API without a dedicated except block.
    """


def encode_cursor(timestamp: datetime, identifier: str) -> str:
    """Return an opaque cursor representing ``(timestamp, identifier)``.

    ``timestamp`` must be timezone-aware; the codec preserves the tz
    offset verbatim. ``identifier`` must be non-empty and free of the
    ``|`` separator so the round-trip is unambiguous.
    """
    if timestamp.tzinfo is None:
        raise CursorError("cursor timestamps must be timezone-aware")
    if not identifier:
        raise CursorError("cursor identifier must be non-empty")
    if "|" in identifier:
        raise CursorError("cursor identifier must not contain '|'")
    raw = f"{timestamp.isoformat()}|{identifier}".encode()
    # ``urlsafe_b64encode`` keeps the cursor inline-safe in URLs and
    # JSON; padding is stripped so the cursor reads cleanly in the
    # query string.
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    """Decode an opaque cursor back into ``(timestamp, identifier)``.

    Raises :class:`CursorError` if the cursor is malformed, the base64
    layer fails, the embedded timestamp is unparseable, or the
    identifier is empty.
    """
    if not cursor:
        raise CursorError("cursor must be non-empty")
    # Restore padding lost by ``encode_cursor`` so b64decode accepts
    # the input regardless of its length.
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError) as exc:
        raise CursorError("cursor base64 invalid") from exc
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CursorError("cursor payload not utf-8") from exc
    if "|" not in decoded:
        raise CursorError("cursor payload missing separator")
    ts_part, _, id_part = decoded.partition("|")
    if not id_part:
        raise CursorError("cursor identifier missing")
    try:
        timestamp = datetime.fromisoformat(ts_part)
    except ValueError as exc:
        raise CursorError("cursor timestamp invalid") from exc
    if timestamp.tzinfo is None:
        raise CursorError("cursor timestamp must be timezone-aware")
    return timestamp, id_part


__all__ = ["CursorError", "decode_cursor", "encode_cursor"]
