"""Rate-limiter port.

【目标】所有跨副本共享的限流原语统一从这里出。业务层（LLM client
装饰器、Git provider、Tool 调用）只依赖 :class:`RateLimiter`，不直
接耦合 Redis / Lua / pybreaker 等基础设施实现。

契约要点
========

* :meth:`RateLimiter.acquire` 是协作式调用：返回
  :class:`RateLimitDecision`，由调用方决定丢弃 / 排队 / 抛错。Port
  本身不主动抛 "denied"——拒绝是正常控制流，不是异常。
* 仅在 **基础设施故障** 时抛 :class:`RateLimiterBackendError`
  （Redis 不可达、Lua 失败等）。这是真正异常，调用方据此决定
  fail-open / fail-closed。
* 实现必须对同一 ``key`` 的并发调用安全，并跨副本原子（Redis 实现
  靠 Lua；in-memory 实现限定单进程）。
* ``key`` 是不透明字符串；命名空间约定见 :mod:`docs/specs/`，Port
  不强制 schema 以保留扩展空间。

L0 多租户约束：业务侧装饰器必须把 ``tenant_id`` 编进 key
（``llm:openrouter:tenant={tid}:model={m}``）；Port 不感知租户。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from meta_agent.core.domain import AgentError, ErrorCategory


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Outcome of a single :meth:`RateLimiter.acquire` call.

    Attributes
    ----------
    allowed:
        ``True`` if the requested cost was consumed; ``False`` if the
        bucket did not have enough tokens. Adapters MUST NOT partially
        consume — either ``cost`` tokens are taken or none are.
    remaining:
        Tokens left in the bucket *after* this call. For denied calls
        this reflects the pre-call snapshot (no tokens were consumed).
        Implementations MAY round/floor this; callers should treat it
        as advisory.
    retry_after_ms:
        Hint for how long the caller should wait before the bucket
        would refill at least ``cost`` tokens. ``None`` when
        ``allowed=True`` or when the implementation cannot estimate.
    """

    allowed: bool
    remaining: int
    retry_after_ms: int | None = None


class RateLimiterBackendError(AgentError):
    """Raised when the limiter's backend itself fails.

    This is distinct from "denied" (which is a normal :class:`RateLimitDecision`
    with ``allowed=False``). Reserved for genuine infrastructure faults
    — Redis disconnect, Lua syntax error, schema mismatch — so callers
    can apply a fail-open or fail-closed policy without parsing message
    strings.
    """

    category = ErrorCategory.EXTERNAL


class RateLimiter(ABC):
    """Cooperative bucket-style rate limiter.

    Concrete implementations live under :mod:`meta_agent.infra.ratelimit`.
    """

    @abstractmethod
    async def acquire(self, key: str, *, cost: int = 1) -> RateLimitDecision:
        """Try to consume ``cost`` tokens for ``key``.

        Parameters
        ----------
        key:
            Opaque bucket identifier. Callers are responsible for
            embedding any tenant / resource scoping into the key.
        cost:
            Number of tokens to consume. Must be ``>= 1``. Implementations
            MAY refuse a single call that would exceed the bucket's
            configured burst capacity (returning ``allowed=False``).

        Returns
        -------
        RateLimitDecision
            The outcome of the attempt. Implementations MUST NOT raise
            for the "denied" case.

        Raises
        ------
        RateLimiterBackendError
            On genuine backend faults (network, Lua error, schema drift).
        """

    async def close(self) -> None:
        """Release any backend resources held by the limiter.

        Default no-op; adapters that hold a connection pool override this.
        """
        return None


__all__ = [
    "RateLimitDecision",
    "RateLimiter",
    "RateLimiterBackendError",
]
