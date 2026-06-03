"""Step-kind-aware model routing decorator + the default static router.

Decorator placement
===================
:class:`RoutingLLMClient` is the **outermost** layer of the LLM stack
in production: it inspects ``request.step_kind``, asks the injected
:class:`LLMRouter` for an override, and rewrites ``request.model``
before delegating inward. Downstream decorators
(``BudgetEnforcing`` → ``RateLimited`` → ``Metered`` →
``CircuitBreaking`` → ``OpenRouter``) all see the routed model, so
budgets / rate limits / usage rows / circuit breakers attribute spend
to the actually-served model rather than the caller's pre-route hint.

Default policy: cheap Chinese models via OpenRouter
====================================================
The default :class:`StaticLLMRouter` mapping prefers
DeepSeek / Qwen / GLM slugs because they cost roughly an order of
magnitude less than the OpenAI / Anthropic premium tier while
remaining strong enough for coding-agent step kinds. Operators
override individual entries via env (see
``meta_agent.worker.bootstrap``). Setting the env to an empty string
disables the override for that step kind, which makes
:meth:`select_model` return ``None`` and the caller's existing model
flows through unchanged.
"""

from __future__ import annotations

from meta_agent.core.ports.llm import (
    LLMClient,
    LLMRequest,
    LLMResponse,
)
from meta_agent.core.ports.llm_router import LLMRouter


class StaticLLMRouter(LLMRouter):
    """Pure-dict :class:`LLMRouter` keyed by ``step_kind``.

    Unknown step kinds return ``None``. Tenant overrides are not
    consulted by this implementation; per-tenant routing belongs in a
    follow-up adapter once we observe real demand.
    """

    def __init__(self, mapping: dict[str, str]) -> None:
        # Defensive copy + light validation so a typo in env config
        # surfaces at boot instead of corrupting an outbound request.
        normalised: dict[str, str] = {}
        for step_kind, model in mapping.items():
            if not step_kind:
                raise ValueError("StaticLLMRouter: step_kind keys must be non-empty")
            if not isinstance(model, str) or not model.strip():
                raise ValueError(
                    f"StaticLLMRouter: model for step_kind {step_kind!r} must be a "
                    f"non-empty string, got {model!r}"
                )
            normalised[step_kind] = model
        self._mapping = normalised

    async def select_model(
        self,
        *,
        step_kind: str,
        tenant_id: str | None = None,
    ) -> str | None:
        return self._mapping.get(step_kind)


class RoutingLLMClient(LLMClient):
    """Decorator that rewrites ``request.model`` based on ``request.step_kind``.

    Behavior:

    * If ``request.step_kind`` is ``None`` or the router returns ``None``,
      the request is forwarded unchanged.
    * Otherwise ``request.model`` is replaced with the router's pick.
      The caller's original ``model`` (if any) is discarded; usage logs
      capture the routed value in ``requested_model`` and the provider's
      served value in ``model``, which is the same convention metered
      LLM rows have used since α.
    """

    def __init__(self, inner: LLMClient, router: LLMRouter) -> None:
        self._inner = inner
        self._router = router

    async def complete(self, request: LLMRequest) -> LLMResponse:
        routed = await self._maybe_route(request)
        return await self._inner.complete(routed)

    async def close(self) -> None:
        await self._inner.close()

    async def _maybe_route(self, request: LLMRequest) -> LLMRequest:
        if request.step_kind is None:
            return request
        # Tenant context propagates through ``contextvars`` rather than
        # the request body; the router currently ignores it but the
        # signature is in place for future tenant-aware overrides.
        target = await self._router.select_model(step_kind=request.step_kind)
        if target is None or target == request.model:
            return request
        return request.model_copy(update={"model": target})


__all__ = [
    "RoutingLLMClient",
    "StaticLLMRouter",
]
