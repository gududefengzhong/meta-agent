"""Built-in graph definitions.

* ``builtin.echo`` — deterministic three-node smoke flow with no
  external dependencies.
* ``builtin.simple_chat`` — single-turn chat completion through the
  :class:`LLMClient` port.
* ``builtin.git_inspect`` — workspace smoke flow that reads ``git log``
  inside the provisioned worktree; demonstrates the L0 isolation path.
* ``builtin.bug_fix`` — first L1 business graph: plan / patch / verify
  / finalize over a per-task worktree.
* ``builtin.code_review`` — second L1 business graph: structured LLM
  review of a caller-supplied unified diff, no workspace required.
"""

from meta_agent.core.orchestration.graphs.bug_fix import (
    BUG_FIX_GRAPH_ID,
    build_bug_fix_graph,
)
from meta_agent.core.orchestration.graphs.code_review import (
    CODE_REVIEW_GRAPH_ID,
    build_code_review_graph,
)
from meta_agent.core.orchestration.graphs.echo import (
    ECHO_GRAPH_ID,
    build_echo_graph,
)
from meta_agent.core.orchestration.graphs.git_inspect import (
    GIT_INSPECT_GRAPH_ID,
    build_git_inspect_graph,
)
from meta_agent.core.orchestration.graphs.simple_chat import (
    SIMPLE_CHAT_GRAPH_ID,
    build_simple_chat_graph,
)

__all__ = [
    "BUG_FIX_GRAPH_ID",
    "CODE_REVIEW_GRAPH_ID",
    "ECHO_GRAPH_ID",
    "GIT_INSPECT_GRAPH_ID",
    "SIMPLE_CHAT_GRAPH_ID",
    "build_bug_fix_graph",
    "build_code_review_graph",
    "build_echo_graph",
    "build_git_inspect_graph",
    "build_simple_chat_graph",
]
