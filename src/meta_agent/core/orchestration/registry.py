"""Routing layer between ``Task`` and a concrete :class:`Graph`.

A task carries two related but distinct concepts:

* ``task_type`` — *what* the task is (a business family or a built-in
  system family). Stable, customer-facing.
* ``graph_id`` — *which graph* should execute this run. May be ``None``
  to mean "use the default graph for ``task_type``".

The registry has two phases:

1. **Registration**: callers register :data:`GraphFactory` callables
   keyed by ``graph_id``, optionally pinning a default for a task type.
   No graph instance exists yet — only the recipe.
2. **Materialization**: at boot, the owner of the registry calls
   :meth:`materialize` with a :class:`GraphDeps` container. Each
   factory is invoked exactly once and the resulting compiled graphs
   are cached. After this, :meth:`resolve` / :meth:`get` return the
   cached :class:`Graph` instances.

This split keeps the core orchestration layer free of any infra
imports: factories receive ``GraphDeps`` (a port-only container) so the
registry itself never needs to know what concrete LLM / queue / store
adapter is wired in.
"""

from __future__ import annotations

from meta_agent.core.domain.task import TaskType
from meta_agent.core.orchestration.deps import GraphDeps, GraphFactory
from meta_agent.core.orchestration.graph import Graph, GraphError


class GraphRegistry:
    """In-process lookup table for orchestration graphs.

    Not thread-safe; the worker owns a single instance and uses it
    from a single asyncio loop.
    """

    def __init__(self) -> None:
        self._factories: dict[str, GraphFactory] = {}
        self._defaults: dict[TaskType, str] = {}
        self._graphs: dict[str, Graph] = {}
        self._materialized: bool = False

    def register(
        self,
        graph_id: str,
        factory: GraphFactory,
        *,
        default_for: TaskType | None = None,
    ) -> None:
        """Register a graph factory under ``graph_id``.

        Optionally pin this graph as the default for ``default_for``.
        Raises :class:`GraphError` on duplicate ``graph_id``, conflicting
        defaults, or registration after materialization.
        """

        if self._materialized:
            raise GraphError("cannot register after materialize()")
        if not graph_id:
            raise GraphError("graph_id must be non-empty")
        if graph_id in self._factories:
            raise GraphError(f"graph {graph_id!r} already registered")
        self._factories[graph_id] = factory
        if default_for is not None:
            if default_for in self._defaults:
                raise GraphError(
                    f"task_type {default_for.value!r} already has a default graph "
                    f"({self._defaults[default_for]!r})"
                )
            self._defaults[default_for] = graph_id

    def materialize(self, deps: GraphDeps) -> None:
        """Invoke every factory exactly once and cache the result.

        Idempotent only against the same :class:`GraphDeps` instance:
        calling :meth:`materialize` twice raises rather than silently
        rebuilding, because the worker must observe a stable mapping.
        """

        if self._materialized:
            raise GraphError("registry already materialized")
        for graph_id, factory in self._factories.items():
            graph = factory(deps)
            if graph.graph_id != graph_id:
                raise GraphError(
                    f"factory for {graph_id!r} returned graph with "
                    f"mismatched graph_id {graph.graph_id!r}"
                )
            graph.compile()
            self._graphs[graph_id] = graph
        self._materialized = True

    def get(self, graph_id: str) -> Graph:
        self._require_materialized()
        if graph_id not in self._graphs:
            raise GraphError(f"unknown graph_id {graph_id!r}")
        return self._graphs[graph_id]

    def resolve(self, task_type: TaskType, graph_id: str | None = None) -> Graph:
        """Resolve the graph to run for a task.

        Explicit ``graph_id`` wins; otherwise the registered default
        for ``task_type`` is used. Raises :class:`GraphError` if the
        registry is not materialized or no graph matches.
        """

        self._require_materialized()
        if graph_id is not None:
            return self.get(graph_id)
        if task_type not in self._defaults:
            raise GraphError(f"no default graph registered for task_type {task_type.value!r}")
        return self._graphs[self._defaults[task_type]]

    def default_graph_id(self, task_type: TaskType) -> str | None:
        return self._defaults.get(task_type)

    @property
    def is_materialized(self) -> bool:
        return self._materialized

    def _require_materialized(self) -> None:
        if not self._materialized:
            raise GraphError("registry not materialized; call materialize(deps) first")
