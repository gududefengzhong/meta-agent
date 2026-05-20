"""PostgreSQL-backed :class:`TokenValidator`.

Stores SHA-256 hashes of tokens, never the raw token, so a database
dump cannot recover live credentials. Rows can be revoked by setting
``revoked_at``. Lookup is by exact hash match (the hash function is
deterministic) so we don't need a separate index over the cleartext.

``last_used_at`` is updated best-effort: the write happens in a
fire-and-forget background task so a slow / failing UPDATE never
delays the validation response. Failures are logged at warning level
and never propagated.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

from meta_agent.core.ports.auth import AuthBackendError, Principal, TokenValidator
from meta_agent.infra.persistence.pool import DatabasePool

logger = logging.getLogger(__name__)


def _hash_token(token: str) -> str:
    """Return the lowercase hex SHA-256 of ``token``.

    The hash is the canonical lookup form; it is what we store in the
    ``api_keys.token_hash`` column. No salt is used: tokens are random
    high-entropy material, not user passwords, so a salt would only
    prevent equality lookups without adding meaningful security.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PgTokenValidator(TokenValidator):
    """asyncpg-backed :class:`TokenValidator`."""

    _LOOKUP = (
        "SELECT tenant_id, principal_id, scopes "
        "FROM api_keys "
        "WHERE token_hash = $1 AND revoked_at IS NULL "
        "LIMIT 1"
    )
    _TOUCH = "UPDATE api_keys SET last_used_at = NOW() WHERE token_hash = $1"

    def __init__(self, pool: DatabasePool, *, touch_last_used: bool = True) -> None:
        self._pool = pool
        self._touch_last_used = touch_last_used

    async def validate(self, token: str) -> Principal | None:
        if not token:
            return None
        token_hash = _hash_token(token)
        try:
            async with self._pool.acquire() as conn:
                row: dict[str, Any] | None = await conn.fetchrow(self._LOOKUP, token_hash)
        except Exception as exc:
            raise AuthBackendError(f"api_keys lookup failed: {exc}") from exc
        if row is None:
            return None
        scopes_raw = row.get("scopes") or ()
        principal = Principal(
            tenant_id=row["tenant_id"],
            principal_id=row["principal_id"],
            scopes=tuple(scopes_raw),
        )
        if self._touch_last_used:
            self._schedule_touch(token_hash)
        return principal

    def _schedule_touch(self, token_hash: str) -> None:
        """Fire-and-forget ``last_used_at`` update; never blocks validate()."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Not in an event loop (shouldn't happen for async validate);
            # skip silently rather than crash.
            return
        task = loop.create_task(self._do_touch(token_hash))
        # Prevent "task was destroyed" warnings without holding a strong
        # reference long-term; the task self-completes quickly.
        task.add_done_callback(_log_touch_error)

    async def _do_touch(self, token_hash: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(self._TOUCH, token_hash)


def _log_touch_error(task: asyncio.Task[None]) -> None:
    exc = task.exception()
    if exc is not None:
        logger.warning("api_keys.last_used_at_touch_failed: %s", exc)


__all__ = ["PgTokenValidator"]
