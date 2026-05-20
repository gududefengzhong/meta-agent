"""Always-closed circuit breaker.

Safe default for dev / test wiring: every call is forwarded to ``fn``;
``should_count`` is ignored. Useful as the production default until the
operator opts in to a real breaker via env.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from meta_agent.core.ports.circuit_breaker import CircuitBreaker

T = TypeVar("T")


class NoopCircuitBreaker(CircuitBreaker):
    """Permits every call; never trips."""

    async def call(
        self,
        key: str,
        fn: Callable[[], Awaitable[T]],
        *,
        should_count: Callable[[BaseException], bool] | None = None,
    ) -> T:
        del key, should_count
        return await fn()


__all__ = ["NoopCircuitBreaker"]
