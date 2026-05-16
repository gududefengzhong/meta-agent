"""OpenRouter adapter configuration.

Values are read from the process environment (the platform secret
manager in production; ``.env`` in local dev — read by the launcher,
never by the application directly, per ``.env.example``). The dataclass
captures exactly what the adapter needs so the rest of the codebase
does not poke at environment variables.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class OpenRouterConfig:
    """Static configuration for :class:`OpenRouterClient`.

    The ``api_key`` is required and must never be logged. The default
    model can be overridden per-request through :class:`LLMRequest`.

    Retry knobs follow exponential backoff with a small jitter cap:
    ``initial_backoff * 2**attempt`` bounded by ``max_backoff``. Only
    transient categories (429 / 5xx / network) are retried.
    """

    api_key: str
    base_url: str = "https://openrouter.ai/api/v1"
    default_model: str = "deepseek/deepseek-chat"
    timeout_seconds: float = 60.0
    max_retries: int = 2
    initial_backoff_seconds: float = 0.5
    max_backoff_seconds: float = 8.0
    referer: str | None = None
    """Optional ``HTTP-Referer`` header value; OpenRouter uses it for attribution."""
    title: str | None = None
    """Optional ``X-Title`` header value; appears in OpenRouter dashboards."""
    extra_headers: dict[str, str] = field(default_factory=dict)
    """Static headers merged into every request. Adapter rejects auth-like keys."""

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        required: bool = True,
    ) -> OpenRouterConfig:
        """Build a config from environment variables.

        ``required=False`` lets callers construct a config when the key
        is unset (returning ``None`` from a factory is more honest, so
        this just raises when ``required`` is ``True`` and the key is
        missing — keep call sites explicit about secrets).
        """
        source = env if env is not None else os.environ
        api_key = source.get("OPENROUTER_API_KEY", "").strip()
        if required and not api_key:
            raise ValueError("OPENROUTER_API_KEY is required but not set in the environment")
        return cls(
            api_key=api_key,
            base_url=source.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/"),
            default_model=source.get("OPENROUTER_DEFAULT_MODEL", "deepseek/deepseek-chat"),
            timeout_seconds=float(source.get("OPENROUTER_TIMEOUT_SECONDS", "60")),
            max_retries=int(source.get("OPENROUTER_MAX_RETRIES", "2")),
            initial_backoff_seconds=float(source.get("OPENROUTER_INITIAL_BACKOFF_SECONDS", "0.5")),
            max_backoff_seconds=float(source.get("OPENROUTER_MAX_BACKOFF_SECONDS", "8")),
            referer=source.get("OPENROUTER_REFERER") or None,
            title=source.get("OPENROUTER_TITLE") or None,
        )
