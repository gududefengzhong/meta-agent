"""Built-in graph definitions.

Currently ships only the ``builtin.echo`` smoke graph; business graphs
join in later phases.
"""

from meta_agent.core.orchestration.graphs.echo import (
    ECHO_GRAPH_ID,
    build_echo_graph,
)

__all__ = ["ECHO_GRAPH_ID", "build_echo_graph"]
