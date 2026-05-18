"""GitHub adapter configuration.

Values are read from the process environment (the platform secret
manager in production; ``.env`` in local dev — read by the launcher,
never by the application directly). The dataclass captures exactly
what the adapter needs so the rest of the codebase does not poke at
environment variables.

The ``token`` is a Personal Access Token / fine-grained token / GitHub
App installation token. v1 is process-level single-token; per-tenant
credential isolation is a later milestone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GitHubGitProviderConfig:
    """Static configuration for :class:`GitHubGitProvider`.

    The ``token`` is required and MUST NOT be logged. Retry knobs follow
    exponential backoff: ``initial_backoff * 2**attempt`` bounded by
    ``max_backoff``. Only transient categories (429 / 5xx / network) are
    retried; auth / invalid-request failures surface immediately.
    """

    token: str
    base_url: str = "https://api.github.com"
    """GitHub REST base URL; override for GitHub Enterprise Server."""
    user_agent: str = "meta-agent/0.0"
    timeout_seconds: float = 30.0
    max_retries: int = 2
    initial_backoff_seconds: float = 0.5
    max_backoff_seconds: float = 8.0

    @classmethod
    def from_env(
        cls,
        env: dict[str, str] | None = None,
        *,
        required: bool = True,
    ) -> GitHubGitProviderConfig:
        """Build a config from environment variables.

        ``required=False`` lets callers construct a config when the
        token is unset (so the worker bootstrap can choose between the
        fake and the real adapter without inspecting env twice).
        """
        source = env if env is not None else os.environ
        token = source.get("META_AGENT_GITHUB_TOKEN", "").strip()
        if required and not token:
            raise ValueError(
                "META_AGENT_GITHUB_TOKEN is required but not set in the environment"
            )
        return cls(
            token=token,
            base_url=source.get("META_AGENT_GITHUB_BASE_URL", "https://api.github.com").rstrip("/"),
            user_agent=source.get("META_AGENT_GITHUB_USER_AGENT", "meta-agent/0.0"),
            timeout_seconds=float(source.get("META_AGENT_GITHUB_TIMEOUT_SECONDS", "30")),
            max_retries=int(source.get("META_AGENT_GITHUB_MAX_RETRIES", "2")),
            initial_backoff_seconds=float(
                source.get("META_AGENT_GITHUB_INITIAL_BACKOFF_SECONDS", "0.5")
            ),
            max_backoff_seconds=float(source.get("META_AGENT_GITHUB_MAX_BACKOFF_SECONDS", "8")),
        )
