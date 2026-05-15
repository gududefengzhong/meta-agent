"""Routing layer between ``Task`` and a concrete :class:`Graph`.

A task carries two related but distinct concepts:

* ``task_type`` — *what* the task is (a business family or a built-in
  system family). Stable, customer-facing.
* ``graph_id`` — *which graph* should execute this run. May be ``None``
  to mean "use the default graph for ``task_type``".

The registry owns both directions: it stores graphs by ``graph_id``
and, separately, the default ``graph_id`` for each ``task_type``.
"""

from __future__ import annotations

from meta_agent.core.domain.task import TaskType
from meta_agent.core.orchestration.graph import Graph, GraphError


class GraphRegistry:
    """In-process lookup table for orchestration graphs."""

    def __init__(self) -> None:
        self._graphs: dict[str, Graph] = {}
        self._defaults: dict[TaskType, str] = {}

    def register(self, graph: Graph, *, default_for: TaskType | None = None) -> None:
        """Register ``graph`` and optionally make it the default for a type."""

        graph.compile()
        if graph.graph_id in self._graphs:
            raise GraphError(f"graph {graph.graph_id!r} already registered")
        self._graphs[graph.graph_id] = graph
        if default_for is not None:
            if default_for in self._defaults:
                raise GraphError(
                    f"task_type {default_for.value!r} already has a default graph "
                    f"({self._defaults[default_for]!r})"
                )
            self._defaults[default_for] = graph.graph_id

    def get(self, graph_id: str) -> Graph:
        if graph_id not in self._graphs:
            raise GraphError(f"unknown graph_id {graph_id!r}")
        return self._graphs[graph_id]

    def resolve(self, task_type: TaskType, graph_id: str | None = None) -> Graph:
        """Resolve the graph to run for a task.

        Explicit ``graph_id`` wins; otherwise the registered default
        for ``task_type`` is used. Raises :class:`GraphError` if no
        graph matches.
        """

        if graph_id is not None:
            return self.get(graph_id)
        if task_type not in self._defaults:
            raise GraphError(f"no default graph registered for task_type {task_type.value!r}")
        return self._graphs[self._defaults[task_type]]

    def default_graph_id(self, task_type: TaskType) -> str | None:
        return self._defaults.get(task_type)
