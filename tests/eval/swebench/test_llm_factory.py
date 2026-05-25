"""Unit tests for :mod:`eval.swebench.llm_factory`."""

from __future__ import annotations

import pytest
from eval.swebench.llm_factory import EvalLLMConfigError, build_default_llm

from meta_agent.infra.llm.openrouter import OpenRouterClient
from meta_agent.infra.llm.redacting import RedactingLLMClient


def test_missing_api_key_raises_clear_error() -> None:
    with pytest.raises(EvalLLMConfigError, match="API key not provided"):
        build_default_llm(env={})


def test_api_key_from_env_picked_up() -> None:
    client = build_default_llm(env={"OPENROUTER_API_KEY": "tok-test"})
    # Outermost layer is redaction when ``redact=True`` (default).
    assert isinstance(client, RedactingLLMClient)


def test_explicit_api_key_overrides_env() -> None:
    client = build_default_llm(
        api_key="explicit-key",
        env={"OPENROUTER_API_KEY": "from-env"},
    )
    assert isinstance(client, RedactingLLMClient)


def test_redact_false_yields_bare_openrouter_client() -> None:
    client = build_default_llm(api_key="k", redact=False)
    assert isinstance(client, OpenRouterClient)
    assert not isinstance(client, RedactingLLMClient)


def test_blank_api_key_treated_as_missing() -> None:
    with pytest.raises(EvalLLMConfigError):
        build_default_llm(env={"OPENROUTER_API_KEY": "   "})
