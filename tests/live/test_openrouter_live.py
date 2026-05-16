"""Live smoke test against the real OpenRouter API.

This test verifies the adapter actually talks to the production endpoint
and that the configured key + default model behave as expected. It is
opt-in:

- Default test runs (``pytest``) deselect it via the ``integration``
  marker, the same gate used for Postgres / Redis tests.
- Even with ``pytest -m integration``, it is skipped when
  ``OPENROUTER_API_KEY`` is unset (so the rest of the integration
  suite still runs in environments without an API key).

The prompt is deliberately tiny and ``max_tokens`` is capped to keep
the cost of every run negligible.
"""

from __future__ import annotations

import os

import pytest

from meta_agent.core.ports.llm import ChatMessage, LLMRequest, MessageRole
from meta_agent.infra.llm.config import OpenRouterConfig
from meta_agent.infra.llm.openrouter import OpenRouterClient

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("OPENROUTER_API_KEY"),
        reason="OPENROUTER_API_KEY not set; skipping live OpenRouter smoke test",
    ),
]


async def test_openrouter_live_smoke_completes_short_prompt() -> None:
    """Send a trivial prompt and assert the adapter returns a non-empty answer.

    The point of this test is contract verification, not output quality:
    we only check that the response is well-formed and that token usage
    came back, which is the contract the worker / graph code relies on.
    """
    config = OpenRouterConfig.from_env()
    client = OpenRouterClient(config)
    try:
        response = await client.complete(
            LLMRequest(
                messages=(
                    ChatMessage(
                        role=MessageRole.SYSTEM,
                        content="You are a terse smoke-test assistant.",
                    ),
                    ChatMessage(
                        role=MessageRole.USER,
                        content="Reply with the single word OK.",
                    ),
                ),
                max_tokens=8,
                temperature=0.0,
            )
        )
    finally:
        await client.close()

    assert isinstance(response.content, str)
    assert response.content.strip() != ""
    assert response.model  # provider echoes the resolved model id
    assert response.finish_reason in {"stop", "length", "other"}
    # Usage is not strictly mandated by the spec but virtually every
    # provider returns it; if a future provider drops it the field is
    # ``None`` rather than absent.
    assert response.usage.total_tokens is None or response.usage.total_tokens >= 1
