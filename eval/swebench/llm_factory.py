"""Minimal LLM client factory for eval runs.

The worker bootstrap wires a full LLM stack (broadcasting + redaction
+ routing + budget + rate-limit + metering + circuit-breaker +
OpenRouter). For benchmark runs we want fewer layers — the eval is
running against ourselves, doesn't need per-call audit rows, and
should not be subject to per-tenant rate limits that exist for
production traffic.

This factory builds the slimmest viable stack:

* :class:`OpenRouterClient` — the actual provider
* :class:`RedactingLLMClient` (optional) — keeps eval logs free of
  secrets in the rare case a SWE-bench fixture leaks one
* :class:`RoutingLLMClient` (optional) — when the operator wants
  per-step-kind model selection during eval (cost control on
  large runs)

All else is deliberately skipped. Production-time wrappers
(circuit-breaker / rate-limit / budget) would just complicate the
eval signal without adding value.

Configuration
=============
The factory reads ``OPENROUTER_API_KEY`` from the environment
(matching the worker convention). Operators driving custom
endpoints (mirror, self-hosted) override ``base_url`` /
``default_model`` via :func:`build_default_llm`'s kwargs.
"""

from __future__ import annotations

import os

from meta_agent.core.ports.llm import LLMClient
from meta_agent.infra.llm.config import OpenRouterConfig
from meta_agent.infra.llm.openrouter import OpenRouterClient
from meta_agent.infra.llm.redacting import RedactingLLMClient
from meta_agent.infra.redaction import Redactor

_DEFAULT_MODEL = "deepseek/deepseek-chat"


class EvalLLMConfigError(Exception):
    """Raised when the LLM factory cannot construct a client (e.g. missing key)."""


def build_default_llm(
    *,
    api_key: str | None = None,
    default_model: str = _DEFAULT_MODEL,
    base_url: str | None = None,
    redact: bool = True,
    env: dict[str, str] | None = None,
) -> LLMClient:
    """Build a slim eval-time :class:`LLMClient`.

    ``api_key`` falls back to ``$OPENROUTER_API_KEY`` (or the
    explicit ``env`` mapping if provided). Missing key raises
    :class:`EvalLLMConfigError` with a clear message.

    ``redact=True`` wraps the OpenRouter client in
    :class:`RedactingLLMClient` so any echoed secrets land scrubbed
    in the eval logs.
    """

    e = env if env is not None else dict(os.environ)
    explicit = api_key.strip() if api_key else ""
    resolved_key = explicit or e.get("OPENROUTER_API_KEY", "").strip()
    if not resolved_key:
        raise EvalLLMConfigError(
            "OpenRouter API key not provided: set OPENROUTER_API_KEY or pass api_key="
        )
    config_kwargs: dict[str, str] = {
        "api_key": resolved_key,
        "default_model": default_model,
    }
    if base_url is not None:
        config_kwargs["base_url"] = base_url
    config = OpenRouterConfig(**config_kwargs)  # type: ignore[arg-type]
    client: LLMClient = OpenRouterClient(config)
    if redact:
        client = RedactingLLMClient(client, redactor=Redactor())
    return client


__all__ = ["EvalLLMConfigError", "build_default_llm"]
