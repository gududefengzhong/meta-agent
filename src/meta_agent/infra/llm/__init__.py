"""LLM adapters.

Currently ships only the OpenRouter HTTP adapter; future providers
(Anthropic native, on-prem vLLM, etc.) implement the same
:class:`meta_agent.core.ports.LLMClient` port and slot in here.
"""

from meta_agent.infra.llm.budget_enforcing import BudgetEnforcingLLMClient
from meta_agent.infra.llm.circuit_breaking import CircuitBreakingLLMClient
from meta_agent.infra.llm.config import OpenRouterConfig
from meta_agent.infra.llm.metered import MeteredLLMClient
from meta_agent.infra.llm.openrouter import OpenRouterClient
from meta_agent.infra.llm.rate_limited import RateLimitedLLMClient

__all__ = [
    "BudgetEnforcingLLMClient",
    "CircuitBreakingLLMClient",
    "MeteredLLMClient",
    "OpenRouterClient",
    "OpenRouterConfig",
    "RateLimitedLLMClient",
]
