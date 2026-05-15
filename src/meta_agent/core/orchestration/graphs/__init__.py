"""Built-in graph definitions.

* ``builtin.echo`` — deterministic three-node smoke flow with no
  external dependencies.
* ``builtin.simple_chat`` — single-turn chat completion through the
  :class:`LLMClient` port.

Business graphs join in later phases.
"""

from meta_agent.core.orchestration.graphs.echo import (
    ECHO_GRAPH_ID,
    build_echo_graph,
)
from meta_agent.core.orchestration.graphs.simple_chat import (
    SIMPLE_CHAT_GRAPH_ID,
    build_simple_chat_graph,
)

__all__ = [
    "ECHO_GRAPH_ID",
    "SIMPLE_CHAT_GRAPH_ID",
    "build_echo_graph",
    "build_simple_chat_graph",
]
