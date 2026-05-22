"""Prompt registry adapters.

Three layers cohabit here:

* :class:`InMemoryPromptRegistry` — single-process default; sufficient
  for unit tests and for booting a worker that seeds its prompts at
  startup and never hot-reloads.
* :class:`CachingPromptRegistry` — TTL-bounded read-through cache that
  wraps any other ``PromptRegistry``. The Postgres adapter is normally
  wrapped in this so per-request fetches do not hit the DB.
* :class:`PgPromptRegistry` — Postgres-backed source of truth, shared
  across worker replicas and the place where operators push new
  prompt versions out-of-band.

Built-in prompt seeds (the ``prompt_id``s that map to the inline
strings that used to live in graph files) are declared in
:mod:`meta_agent.infra.prompt_registry.seeds`; bootstrap calls the
``ensure_seeded`` helper against the production registry on startup so
fresh deployments are usable without manual DB writes.
"""

from meta_agent.infra.prompt_registry.caching import CachingPromptRegistry
from meta_agent.infra.prompt_registry.in_memory import InMemoryPromptRegistry
from meta_agent.infra.prompt_registry.postgres import PgPromptRegistry
from meta_agent.infra.prompt_registry.seeds import (
    BUILTIN_PROMPT_SEEDS,
    PromptSeed,
    ensure_seeded,
)

__all__ = [
    "BUILTIN_PROMPT_SEEDS",
    "CachingPromptRegistry",
    "InMemoryPromptRegistry",
    "PgPromptRegistry",
    "PromptSeed",
    "ensure_seeded",
]
