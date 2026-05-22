"""LLM router port — pick the model for each step kind.

Phase β+ PR 4 introduces step-kind-aware multi-model routing so a
single task can spread its LLM spend across different models without
the graph code knowing anything about provider slugs. The graph tags
each call with a coarse ``step_kind`` ("plan" / "edit" / "review" /
"chat" / "observe"); the routing decorator at the top of the LLM
stack asks this port which model to use for that tag.

Resolution semantics:

* :meth:`select_model` returns the provider-specific model id to use
  (e.g. OpenRouter's ``deepseek/deepseek-chat`` slug), or ``None`` to
  leave the request's existing ``model`` unchanged.
* Implementations MUST NOT raise on unknown ``step_kind``; an unknown
  tag is normal early-adoption behavior and should return ``None`` so
  the LLM client falls back to the caller's model or the provider
  default.

Tenant override: the optional ``tenant_id`` argument lets future
implementations swap models on a per-tenant basis (e.g. a tenant
sponsoring premium models). The default static implementation
ignores it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class LLMRouter(ABC):
    """Map ``step_kind`` (and optional tenant context) to a model id."""

    @abstractmethod
    async def select_model(
        self,
        *,
        step_kind: str,
        tenant_id: str | None = None,
    ) -> str | None:
        """Return the model id for this step, or ``None`` for no override."""
