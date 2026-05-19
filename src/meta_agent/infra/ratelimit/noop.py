"""Always-allow rate limiter.

Useful as a default in dev / test wiring and as the safe fallback when
no backend has been explicitly configured. The returned
:class:`RateLimitDecision` reports a very large ``remaining`` so callers
that surface the number do not need a special case.
"""

from __future__ import annotations

from typing import Final

from meta_agent.core.ports.rate_limiter import RateLimitDecision, RateLimiter

_SENTINEL_REMAINING: Final[int] = 1_000_000_000


class NoopRateLimiter(RateLimiter):
    """Permits every call; never raises."""

    async def acquire(self, key: str, *, cost: int = 1) -> RateLimitDecision:
        if cost < 1:
            raise ValueError("cost must be >= 1")
        return RateLimitDecision(
            allowed=True,
            remaining=_SENTINEL_REMAINING,
            retry_after_ms=None,
        )


__all__ = ["NoopRateLimiter"]
