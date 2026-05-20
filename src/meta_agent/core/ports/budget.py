"""Budget-enforcer port.

【目标】跨副本一致的「本月已用 / 是否超额」闸门。和
:class:`RateLimiter` 的"是否允许当下这次调用"语义互补：
:class:`BudgetEnforcer` 看的是 **窗口聚合**（默认按月），命中即拒，
重试无意义直到窗口翻页。

契约要点
========

* :meth:`BudgetEnforcer.check` 返回 :class:`BudgetDecision`；拒绝是
  正常控制流，不抛异常。和 :class:`RateLimiter` 对齐。
* 仅 **基础设施故障**（聚合查询失败、连接断开等）抛
  :class:`BudgetBackendError`。调用方据此选择 fail-open / fail-closed。
* :class:`BudgetUsage` 同时承载 ``tokens_used`` 与
  ``cost_usd_micros_used``。当前阶段 pricing 流水线未落地，
  ``cost_usd_micros`` 在 ``llm_usage_logs`` 中可能始终为 NULL；
  实现应把 NULL 当作 0 求和，cost 字段穿透到 decision 中以便
  未来 cap on cost 时无需改 port。
* 阶段 α 仅强制 ``tokens_used`` 上限；``cost_usd_micros_used`` 字段
  存在但不被 decorator 用作判定。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from meta_agent.core.domain import AgentError, ErrorCategory


@dataclass(frozen=True, slots=True)
class BudgetUsage:
    """Aggregated consumption snapshot for one tenant in one window.

    Attributes
    ----------
    tokens_used:
        Sum of ``total_tokens`` across every successful + failed
        ``llm_usage_logs`` row in the window. ``None``-valued rows count
        as 0.
    cost_usd_micros_used:
        Sum of ``cost_usd_micros``. ``None``-valued rows count as 0.
        Stays 0 in deployments without a pricing pipeline.
    """

    tokens_used: int
    cost_usd_micros_used: int


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    """Outcome of a single :meth:`BudgetEnforcer.check` call.

    Attributes
    ----------
    allowed:
        ``True`` if the tenant is still under every configured cap.
    usage:
        Pre-call consumption snapshot. Reported regardless of outcome
        so callers can surface "X / Y used" in errors and dashboards.
    limit_tokens:
        Configured monthly token cap, or ``None`` if no cap is enforced.
        Mirrors the deployment's enforcer configuration.
    """

    allowed: bool
    usage: BudgetUsage
    limit_tokens: int | None = None


class BudgetBackendError(AgentError):
    """Raised when the enforcer's backend itself fails.

    Distinct from "denied" (which is a normal :class:`BudgetDecision`
    with ``allowed=False``). Reserved for genuine infrastructure
    faults — database unreachable, aggregation query error — so the
    decorator can apply a fail-open / fail-closed policy without
    parsing message strings.
    """

    category = ErrorCategory.EXTERNAL


class BudgetEnforcer(ABC):
    """Tenant-scoped budget check.

    Concrete implementations live under :mod:`meta_agent.infra.budget`.
    Adapters MUST be safe to call concurrently for the same
    ``tenant_id``; in-process caching layers are part of the decorator,
    not the port.
    """

    @abstractmethod
    async def check(self, tenant_id: str) -> BudgetDecision:
        """Decide whether ``tenant_id`` may make one more LLM call.

        Parameters
        ----------
        tenant_id:
            Tenant whose monthly aggregation should be inspected.

        Returns
        -------
        BudgetDecision
            ``allowed=True`` when no configured cap is breached.
            Implementations MUST NOT raise for the "denied" case.

        Raises
        ------
        BudgetBackendError
            On genuine backend faults (database connectivity,
            aggregation query error, schema drift).
        """

    async def close(self) -> None:
        """Release any backend resources held by the enforcer.

        Default no-op; adapters that hold a connection pool override
        this.
        """
        return None


__all__ = [
    "BudgetBackendError",
    "BudgetDecision",
    "BudgetEnforcer",
    "BudgetUsage",
]
