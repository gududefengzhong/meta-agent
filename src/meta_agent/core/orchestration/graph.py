"""Minimal LangGraph-style runtime.

The runtime is deliberately small (a few dozen lines of logic) so the
core stays auditable and free of upstream API churn. A :class:`Graph`
is a static description: nodes (async functions ``state -> NodeResult``)
plus edges (either a fixed destination or a router function). Calling
:meth:`Graph.step` once executes the node currently pointed at by
``state.current_node`` and returns the next :class:`TaskRunState`; the
worker loop drives this until ``state.finished`` becomes true.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from meta_agent.core.orchestration.state import END, START, TaskRunState

NodeFn = Callable[[TaskRunState], Awaitable["NodeResult"]]
RouterFn = Callable[[TaskRunState], str]


@dataclass(frozen=True)
class NodeResult:
    """Outcome of a single node invocation.

    ``data_update`` is shallow-merged into the state's ``data``.
    ``next_node`` may pin an explicit destination; when ``None`` the
    graph's registered edge or router for the current node decides.
    """

    data_update: dict[str, object] = field(default_factory=dict)
    next_node: str | None = None


@dataclass(frozen=True)
class _Edge:
    static: str | None
    router: RouterFn | None


class GraphError(Exception):
    """Raised on graph definition or execution mistakes."""


class Graph:
    """A compiled, executable directed graph of async nodes."""

    def __init__(self, graph_id: str) -> None:
        if not graph_id:
            raise GraphError("graph_id must be non-empty")
        self._graph_id = graph_id
        self._nodes: dict[str, NodeFn] = {}
        self._edges: dict[str, _Edge] = {}
        self._entry: str | None = None

    @property
    def graph_id(self) -> str:
        return self._graph_id

    def add_node(self, name: str, fn: NodeFn) -> None:
        if name in {START, END}:
            raise GraphError(f"{name!r} is a reserved sentinel")
        if name in self._nodes:
            raise GraphError(f"node {name!r} already registered")
        self._nodes[name] = fn

    def set_entry(self, name: str) -> None:
        if name not in self._nodes:
            raise GraphError(f"entry node {name!r} is not registered")
        self._entry = name

    def add_edge(self, src: str, dst: str) -> None:
        self._require_known(src)
        if dst != END:
            self._require_known(dst)
        if src in self._edges:
            raise GraphError(f"edge from {src!r} already declared")
        self._edges[src] = _Edge(static=dst, router=None)

    def add_conditional(self, src: str, router: RouterFn) -> None:
        self._require_known(src)
        if src in self._edges:
            raise GraphError(f"edge from {src!r} already declared")
        self._edges[src] = _Edge(static=None, router=router)

    def compile(self) -> None:
        """Validate the graph definition.

        Raises :class:`GraphError` if the entry is missing, any node
        lacks an outgoing edge, or an edge points to an unknown node.
        """

        if self._entry is None:
            raise GraphError("entry node not set")
        for name in self._nodes:
            if name not in self._edges:
                raise GraphError(f"node {name!r} has no outgoing edge")

    def _require_known(self, name: str) -> None:
        if name not in self._nodes:
            raise GraphError(f"node {name!r} is not registered")

    def _resolve_next(self, current: str, state: TaskRunState, override: str | None) -> str:
        if override is not None:
            if override != END and override not in self._nodes:
                raise GraphError(f"node {current!r} returned unknown next_node {override!r}")
            return override
        edge = self._edges[current]
        if edge.static is not None:
            return edge.static
        assert edge.router is not None
        dst = edge.router(state)
        if dst != END and dst not in self._nodes:
            raise GraphError(f"router of {current!r} returned unknown next_node {dst!r}")
        return dst

    async def step(self, state: TaskRunState) -> TaskRunState:
        """Execute one node and return the resulting state.

        Calling ``step`` on a finished state is a no-op (returns the
        same instance). If the state is at :data:`START`, the entry
        node is executed; otherwise ``state.current_node`` is.
        """

        if state.finished:
            return state
        if self._entry is None:
            raise GraphError("graph is not compiled (entry missing)")
        current = self._entry if state.current_node == START else state.current_node
        if current not in self._nodes:
            raise GraphError(f"state points at unknown node {current!r}")
        result = await self._nodes[current](state)
        advanced = state.advance(next_node=current, data_update=result.data_update)
        next_node = self._resolve_next(current, advanced, result.next_node)
        return advanced.model_copy(
            update={
                "current_node": next_node,
                "finished": next_node == END,
            }
        )

    async def run(self, state: TaskRunState, *, max_steps: int = 1000) -> TaskRunState:
        """Drive :meth:`step` until the state is finished or the cap is hit."""

        steps = 0
        while not state.finished:
            if steps >= max_steps:
                raise GraphError(f"graph exceeded max_steps={max_steps}")
            state = await self.step(state)
            steps += 1
        return state
