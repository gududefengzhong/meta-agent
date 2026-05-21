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
from typing import TYPE_CHECKING

from meta_agent.core.orchestration.graph import Graph
from meta_agent.core.ports.git_provider import GitProvider
from meta_agent.core.ports.llm import LLMClient

if TYPE_CHECKING:
    from meta_agent.core.capabilities.executor import ToolExecutor
    from meta_agent.core.capabilities.registry import ToolRegistry


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
    git_push_token: str | None = None
    """Bearer token for ``git push`` over HTTPS.

    Injected at boot from the same secret as the GitHub adapter token
    so a single credential covers both PR creation (port-mediated) and
    pushing local commits (subprocess-mediated). ``None`` disables push:
    bug-fix-style graphs fall back to a local-only commit and emit a
    ``push_skip_reason`` in their output. The token MUST be passed to
    ``git`` via the environment, never on the command line.
    """
    tool_registry: ToolRegistry | None = None
    """Static catalogue of tools available to tool-use graphs.

    Populated at boot together with :attr:`tool_executor`. Graphs that
    do not advertise tools to the LLM (e.g. ``simple_chat``) ignore
    this field; tool-use graphs (``shell_agent``) raise
    :class:`GraphError` when it is ``None``.
    """
    tool_executor: ToolExecutor | None = None
    """Dispatch seam translating :class:`ToolCall` -> :class:`ToolResult`.

    Always paired with :attr:`tool_registry`; the executor binds the
    same registry instance so that registry mutation after boot is the
    only failure mode worth defending against.
    """


GraphFactory = Callable[[GraphDeps], Graph]
"""Signature of every registered graph builder.

A factory must be pure: given the same ``GraphDeps``, it must produce
an equivalent compiled :class:`Graph`. Side-effects (network, files,
random state) are forbidden — the registry calls factories exactly
once at materialization and caches the resulting graph.
"""
