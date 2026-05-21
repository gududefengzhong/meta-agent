"""Tool registry seam: name → handler dispatch table.

The registry is the only place that knows the static set of tools
available to an agent loop. Graph nodes ask the registry for the
``tuple[ToolSpec, ...]`` to advertise to the LLM, and the executor
asks it for the handler to invoke when the LLM emits a
:class:`ToolCall`.

Design notes:

* Registration is explicit and one-shot. The registry refuses
  duplicate names (raises :class:`ToolValidationError`) so configuration
  drift between the spec advertised to the LLM and the handler list
  is caught at boot, not silently shadowed.
* The registry is the natural mount point for future `MCP Tool` /
  remote-tool adapters: each registered handler is a single async
  callable, so an MCP-proxied tool plugs in next to a local tool
  without the executor or graph noticing.
* Handlers receive a :class:`ToolContext` so they can validate paths
  against the workspace and propagate ``tenant_id`` / ``trace_id``
  into any subprocess they spawn. Per-tenant policy enforcement
  (rate limiting, audit logging) belongs in decorator handlers
  composed around the registry, not inside it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from meta_agent.core.ports.tools import (
    ToolCall,
    ToolContext,
    ToolNotFoundError,
    ToolResult,
    ToolSpec,
    ToolValidationError,
)

ToolHandler = Callable[[ToolCall, ToolContext], Awaitable[ToolResult]]
"""Signature of every registered tool handler.

The handler MUST return a :class:`ToolResult` for every input that
type-checks against :class:`ToolCall`; argument-level validation
failures should raise :class:`ToolValidationError` so the executor
can convert them into a uniformly-shaped error observation.
"""


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    """A tool's wire-shape spec paired with its runtime handler."""

    spec: ToolSpec
    handler: ToolHandler


class ToolRegistry:
    """In-process registry of available tools, keyed by tool name.

    Not thread-safe; the registry is expected to be populated at
    worker boot and treated as immutable thereafter. Mutation after
    materialization is a programming error, not a runtime feature.
    """

    def __init__(self) -> None:
        self._tools: dict[str, RegisteredTool] = {}

    def register(self, spec: ToolSpec, handler: ToolHandler) -> None:
        """Add a tool to the registry.

        Raises:
            ToolValidationError: another tool is already registered
                under ``spec.name``. Names are global within a single
                registry; collisions almost always mean a configuration
                bug at boot.
        """

        if spec.name in self._tools:
            raise ToolValidationError(
                f"tool {spec.name!r} is already registered; names must be unique within a registry"
            )
        self._tools[spec.name] = RegisteredTool(spec=spec, handler=handler)

    def get(self, name: str) -> RegisteredTool:
        """Return the tool registered under ``name``.

        Raises:
            ToolNotFoundError: no tool registered under ``name``.
        """

        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotFoundError(f"no tool registered under name {name!r}") from exc

    def list_specs(self) -> tuple[ToolSpec, ...]:
        """Return the specs of all registered tools, sorted by name.

        Sorted output keeps the spec list deterministic across runs,
        which matters for prompt caching and for the LLM seeing a
        stable tool order.
        """

        return tuple(self._tools[name].spec for name in sorted(self._tools))

    def names(self) -> frozenset[str]:
        """Return the set of registered tool names."""

        return frozenset(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools
