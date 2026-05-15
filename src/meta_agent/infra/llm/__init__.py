"""LLM adapters.

Currently ships only the OpenRouter HTTP adapter; future providers
(Anthropic native, on-prem vLLM, etc.) implement the same
:class:`meta_agent.core.ports.LLMClient` port and slot in here.
"""

from meta_agent.infra.llm.config import OpenRouterConfig
from meta_agent.infra.llm.openrouter import OpenRouterClient

__all__ = ["OpenRouterClient", "OpenRouterConfig"]
