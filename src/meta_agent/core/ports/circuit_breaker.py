"""Circuit-breaker port.

【目标】给所有跨副本共享的外部依赖（LLM Provider / Git Provider /
Tool）一个统一的「快速失败」入口。业务侧装饰器只依赖
:class:`CircuitBreaker`，不耦合 pybreaker / Redis 计数等具体实现。

契约要点
========

* :meth:`CircuitBreaker.call` 是 *接管* 风格：调用方把要保护的
  ``async`` 调用作为 ``fn`` 传进来，由 breaker 决定是否真的执行。
  这避免了 ``acquire`` / ``notify`` 两步暴露给业务层，也让"已被
  熔断"和"刚刚被熔断"在同一个调用点表达。
* breaker 在 :class:`CircuitBreakerState.OPEN` 状态下不调用 ``fn``，
  直接抛 :class:`CircuitBreakerOpenError`。这是预期内的控制流，
  不是基础设施故障。
* breaker 自己的存储 / 时钟 / 计数失败时抛
  :class:`CircuitBreakerBackendError`。装饰器据此选择 fail-open /
  fail-closed，和限流的 backend-error 模型对齐。
* ``key`` 是不透明字符串；命名空间由调用方决定（建议
  ``llm:openrouter:tenant={tid}:model={m}``，与 :mod:`rate_limiter`
  对齐以便排查时一眼能配对）。
* 失败计入与否由调用方通过 ``should_count`` 决策；典型用法是把
  限流 deny / 4xx 校验失败排除在外，避免误熔断。

L0 多租户约束：breaker 不感知租户，必须由业务侧把 ``tenant_id``
编进 key；与 :class:`RateLimiter` 同样的责任划分。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TypeVar

from meta_agent.core.domain import AgentError, ErrorCategory

T = TypeVar("T")


class CircuitBreakerState(StrEnum):
    """Three-state machine of a single breaker key.

    * ``CLOSED``    — calls flow through; failures are counted.
    * ``OPEN``      — calls fail fast; no traffic reaches the downstream.
    * ``HALF_OPEN`` — a single probe call is allowed; its result decides
      whether to transition back to ``CLOSED`` or stay ``OPEN``.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(AgentError):
    """Raised by :meth:`CircuitBreaker.call` when the breaker is open.

    Carries the breaker ``key`` and a hint for how long the caller
    should wait before retrying (``retry_after_ms``). Adapters may
    leave ``retry_after_ms`` ``None`` when the cooldown cannot be
    estimated (e.g. when running against a stub backend in tests).
    """

    category = ErrorCategory.TRANSIENT

    def __init__(
        self,
        message: str,
        *,
        key: str,
        retry_after_ms: int | None = None,
    ) -> None:
        super().__init__(message)
        self.key = key
        self.retry_after_ms = retry_after_ms


class CircuitBreakerBackendError(AgentError):
    """Raised when the breaker's own backend (storage / clock) fails.

    Distinct from :class:`CircuitBreakerOpenError`: the latter is a
    normal control-flow outcome ("breaker is open, fail fast"), this
    is a genuine infrastructure fault (Redis disconnect, schema drift).
    Callers apply fail-open or fail-closed policy based on which one
    they see.
    """

    category = ErrorCategory.EXTERNAL


class CircuitBreaker(ABC):
    """Cooperative key-scoped circuit breaker.

    Concrete implementations live under :mod:`meta_agent.infra.circuitbreaker`.
    """

    @abstractmethod
    async def call(
        self,
        key: str,
        fn: Callable[[], Awaitable[T]],
        *,
        should_count: Callable[[BaseException], bool] | None = None,
    ) -> T:
        """Run ``fn`` under the protection of breaker ``key``.

        Parameters
        ----------
        key:
            Opaque breaker identifier. Callers are responsible for
            embedding any tenant / resource scoping into the key.
        fn:
            Zero-argument awaitable producing the wrapped call's
            result. Awaited at most once.
        should_count:
            Predicate deciding whether an exception raised by ``fn``
            counts as a downstream failure. Default behaviour
            (when ``None``) is to count every exception. Callers
            typically pass a predicate that excludes their own
            validation / not-found / rate-limit errors so they do not
            inflate the failure counter.

        Returns
        -------
        T
            Whatever ``fn`` returns on success.

        Raises
        ------
        CircuitBreakerOpenError
            When the breaker is in ``OPEN`` state at call time, or
            when a single ``HALF_OPEN`` probe is already in flight.
        CircuitBreakerBackendError
            On genuine backend faults (Redis disconnect, schema drift).
        """

    async def close(self) -> None:
        """Release any backend resources held by the breaker.

        Default no-op; adapters that hold a connection pool override this.
        """
        return None


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerBackendError",
    "CircuitBreakerOpenError",
    "CircuitBreakerState",
]
