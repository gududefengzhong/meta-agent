"""Dependencies injected into orchestration graphs at materialization.

Graphs are declared as factories ``(GraphDeps) -> Graph`` so the core
layer can stay free of infra imports: a graph that needs an LLM never
references ``meta_agent.infra.llm`` directly, it just reads it from the
:class:`GraphDeps` container passed in at boot time.

The container is intentionally small. New capabilities (tool registry,
secret broker, ...) are added here as optional fields; graphs that do
not need them simply ignore them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from meta_agent.core.orchestration.graph import Graph
from meta_agent.core.ports.git_provider import GitProvider
from meta_agent.core.ports.llm import LLMClient


@dataclass(frozen=True, slots=True)
class GraphDeps:
    """Capabilities injected into graph factories.

    The container is frozen so that materialization is hash-stable and
    cannot be mutated underneath a graph mid-run. Graphs receive the
    same instance for the entire process lifetime.

    Optional fields default to ``None`` so existing graphs that do not
    need them stay constructable; graphs that *require* an optional
    capability must guard for ``None`` and raise :class:`GraphError`.
    """

    llm: LLMClient
    git_provider: GitProvider | None = None


GraphFactory = Callable[[GraphDeps], Graph]
"""Signature of every registered graph builder.

A factory must be pure: given the same ``GraphDeps``, it must produce
an equivalent compiled :class:`Graph`. Side-effects (network, files,
random state) are forbidden — the registry calls factories exactly
once at materialization and caches the resulting graph.
"""
