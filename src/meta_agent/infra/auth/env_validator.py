"""Env-driven :class:`TokenValidator`.

Parses a CSV from one env var into an in-memory token → :class:`Principal`
table. Suitable for dev / CI / local smoke; production deployments
should use the DB-backed adapter so keys can be rotated without
restarts.

Format
======

``META_AGENT_API_KEYS=token1:tenant1:principal1[:scope1,scope2],token2:...``

* Comma-separated entries; whitespace around entries is stripped.
* Inside an entry, fields are colon-separated: ``token:tenant:principal[:scopes]``.
* Scopes are optional; comma-separated inside the scope field is **not**
  supported (the entry separator is comma too); use semicolons instead:
  ``token:tenant:principal:read;write``. Most α deployments will not need
  scopes at all.

The raw env value MUST NOT appear in logs; this module never logs tokens
even on parse failure. Comparisons use :func:`hmac.compare_digest` to
avoid timing oracles.
"""

from __future__ import annotations

import hmac
from typing import Final

from meta_agent.core.ports.auth import Principal, TokenValidator

_FIELD_SEP: Final[str] = ":"
_ENTRY_SEP: Final[str] = ","
_SCOPE_SEP: Final[str] = ";"


def _parse_entries(raw: str) -> dict[str, Principal]:
    """Return a token → principal map. Empty / blank input yields ``{}``."""
    table: dict[str, Principal] = {}
    if not raw:
        return table
    for raw_entry in raw.split(_ENTRY_SEP):
        entry = raw_entry.strip()
        if not entry:
            continue
        parts = entry.split(_FIELD_SEP)
        if len(parts) < 3 or len(parts) > 4:
            raise ValueError("META_AGENT_API_KEYS entry must be 'token:tenant:principal[:scopes]'")
        token, tenant, principal_id, *scope_part = parts
        if not token or not tenant or not principal_id:
            raise ValueError("META_AGENT_API_KEYS entry has empty token / tenant / principal")
        scopes: tuple[str, ...] = ()
        if scope_part and scope_part[0]:
            scopes = tuple(s.strip() for s in scope_part[0].split(_SCOPE_SEP) if s.strip())
        table[token] = Principal(
            tenant_id=tenant,
            principal_id=principal_id,
            scopes=scopes,
        )
    return table


class EnvTokenValidator(TokenValidator):
    """In-memory :class:`TokenValidator` parsed from a CSV string."""

    def __init__(self, entries: str) -> None:
        """Construct from the raw env-string format documented above."""
        self._table = _parse_entries(entries)

    async def validate(self, token: str) -> Principal | None:
        """Constant-time lookup; never raises (parsing happens at construction)."""
        if not token:
            return None
        # ``hmac.compare_digest`` only protects pairwise compares. We
        # iterate the table to keep wall time roughly independent of
        # which entry matches; the size is small (typically <10 keys
        # in dev) so the linear scan is fine.
        match: Principal | None = None
        for stored_token, principal in self._table.items():
            if hmac.compare_digest(token, stored_token):
                match = principal
        return match

    @property
    def size(self) -> int:
        """Number of configured tokens; intended for introspection / metrics."""
        return len(self._table)


__all__ = ["EnvTokenValidator"]
