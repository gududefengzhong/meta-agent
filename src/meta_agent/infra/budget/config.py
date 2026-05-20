"""Env-driven configuration + factory for :class:`BudgetEnforcer`.

Picks one of:

* ``noop``     — :class:`NoopBudgetEnforcer` (default; current behaviour unchanged)
* ``llm_usage`` — :class:`LLMUsageAggregatorBudgetEnforcer` (queries ``llm_usage_logs``)

Env variables
=============

==================================== ============================================
``META_AGENT_BUDGET_BACKEND``        ``noop`` / ``llm_usage`` (default ``noop``)
``META_AGENT_BUDGET_MAX_TOKENS``     Monthly token cap; ``0`` / unset disables
``META_AGENT_BUDGET_CACHE_TTL_S``    Decorator-side cache TTL (default ``10``)
``META_AGENT_BUDGET_FAIL_OPEN``      ``true`` / ``false`` (default ``true``)
==================================== ============================================

The defaults keep budgets off by default so existing deployments stay
unchanged after the decorator lands; operators opt in by setting both
``BACKEND=llm_usage`` and ``MAX_TOKENS>0``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final, Literal

from meta_agent.core.ports.budget import BudgetEnforcer
from meta_agent.core.ports.llm_usage import LLMUsageRepository
from meta_agent.infra.budget.llm_usage_aggregator import LLMUsageAggregatorBudgetEnforcer
from meta_agent.infra.budget.noop import NoopBudgetEnforcer

_BACKEND_ENV: Final[str] = "META_AGENT_BUDGET_BACKEND"
_MAX_TOKENS_ENV: Final[str] = "META_AGENT_BUDGET_MAX_TOKENS"
_CACHE_TTL_ENV: Final[str] = "META_AGENT_BUDGET_CACHE_TTL_S"
_FAIL_OPEN_ENV: Final[str] = "META_AGENT_BUDGET_FAIL_OPEN"

_DEFAULT_BACKEND: Final[str] = "noop"
_DEFAULT_MAX_TOKENS: Final[int] = 0  # 0 == disabled
_DEFAULT_CACHE_TTL: Final[float] = 10.0
_DEFAULT_FAIL_OPEN: Final[bool] = True

Backend = Literal["noop", "llm_usage"]
_SUPPORTED_BACKENDS: Final[tuple[Backend, ...]] = ("noop", "llm_usage")


def _parse_bool(raw: str, *, env_name: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{env_name}={raw!r} is not a boolean")


@dataclass(frozen=True, slots=True)
class BudgetConfig:
    """Parsed env settings for the budget-enforcer factory."""

    backend: Backend
    max_tokens_per_month: int
    cache_ttl_s: float
    fail_open: bool

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> BudgetConfig:
        source: dict[str, str] = dict(env if env is not None else os.environ)
        backend_raw = source.get(_BACKEND_ENV, _DEFAULT_BACKEND).strip().lower()
        if backend_raw not in _SUPPORTED_BACKENDS:
            raise ValueError(f"{_BACKEND_ENV}={backend_raw!r} not in {_SUPPORTED_BACKENDS}")
        max_tokens_raw = source.get(_MAX_TOKENS_ENV, str(_DEFAULT_MAX_TOKENS))
        try:
            max_tokens = int(max_tokens_raw)
        except ValueError as exc:
            raise ValueError(f"{_MAX_TOKENS_ENV}={max_tokens_raw!r} is not an int") from exc
        if max_tokens < 0:
            raise ValueError(f"{_MAX_TOKENS_ENV}={max_tokens} must be >= 0")
        ttl_raw = source.get(_CACHE_TTL_ENV, str(_DEFAULT_CACHE_TTL))
        try:
            ttl = float(ttl_raw)
        except ValueError as exc:
            raise ValueError(f"{_CACHE_TTL_ENV}={ttl_raw!r} is not a float") from exc
        if ttl < 0:
            raise ValueError(f"{_CACHE_TTL_ENV}={ttl} must be >= 0")
        fail_open_raw = source.get(_FAIL_OPEN_ENV)
        fail_open = (
            _DEFAULT_FAIL_OPEN
            if fail_open_raw is None
            else _parse_bool(fail_open_raw, env_name=_FAIL_OPEN_ENV)
        )
        return cls(
            backend=backend_raw,
            max_tokens_per_month=max_tokens,
            cache_ttl_s=ttl,
            fail_open=fail_open,
        )


def build_budget_enforcer_from_config(
    config: BudgetConfig,
    *,
    usage_repo: LLMUsageRepository | None = None,
) -> BudgetEnforcer:
    """Materialise a :class:`BudgetEnforcer` from a parsed config.

    Parameters
    ----------
    config:
        Result of :meth:`BudgetConfig.from_env`.
    usage_repo:
        Required when ``config.backend == "llm_usage"``; ignored otherwise.
        Callers should pass the same :class:`LLMUsageRepository` that
        :class:`MeteredLLMClient` writes to so reads see fresh data.

    Raises
    ------
    ValueError
        If ``backend == "llm_usage"`` but no usage repo was provided.
    """

    if config.backend == "noop":
        return NoopBudgetEnforcer()
    if config.backend == "llm_usage":
        if usage_repo is None:
            raise ValueError(
                f"{_BACKEND_ENV}=llm_usage requires an LLMUsageRepository to be passed in"
            )
        limit = config.max_tokens_per_month or None
        return LLMUsageAggregatorBudgetEnforcer(usage_repo, limit_tokens=limit)
    # mypy: ``Backend`` Literal guarantees we don't reach here.
    raise AssertionError(f"unreachable backend={config.backend!r}")


__all__ = [
    "Backend",
    "BudgetConfig",
    "build_budget_enforcer_from_config",
]
