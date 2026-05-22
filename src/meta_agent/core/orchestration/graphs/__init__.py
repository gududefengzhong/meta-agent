"""Built-in graph definitions.

* ``builtin.echo`` — deterministic three-node smoke flow with no
  external dependencies.
* ``builtin.simple_chat`` — single-turn chat completion through the
  :class:`LLMClient` port.
* ``builtin.git_inspect`` — workspace smoke flow that reads ``git log``
  inside the provisioned worktree; demonstrates the L0 isolation path.
* ``builtin.bug_fix`` — first L1 business graph: plan / patch / verify
  / finalize over a per-task worktree.
* ``builtin.bug_fix_v2`` — Phase β start: minimal tool-use bug-fix loop
  over the same per-task worktree, with deterministic verify.
* ``builtin.code_review`` — second L1 business graph: structured LLM
  review of a caller-supplied unified diff, no workspace required.
* ``builtin.auto_pr`` — third L1 business graph: publish a feature-branch
  commit as a pull request via the :class:`GitProvider` port.
* ``builtin.shell_agent`` — Phase β tool-use loop: plan / tool_call /
  observe iteration against the injected :class:`ToolRegistry`.
* ``builtin.feature_impl`` — Phase β+ first track: shell_agent under a
  feature-implementation framing, exposing ``TaskType.FEATURE_IMPL``.
"""

from meta_agent.core.orchestration.graphs.auto_pr import (
    AUTO_PR_GRAPH_ID,
    build_auto_pr_graph,
)
from meta_agent.core.orchestration.graphs.bug_fix import (
    BUG_FIX_GRAPH_ID,
    build_bug_fix_graph,
)
from meta_agent.core.orchestration.graphs.bug_fix_v2 import (
    BUG_FIX_V2_GRAPH_ID,
    build_bug_fix_v2_graph,
)
from meta_agent.core.orchestration.graphs.code_review import (
    CODE_REVIEW_GRAPH_ID,
    build_code_review_graph,
)
from meta_agent.core.orchestration.graphs.echo import (
    ECHO_GRAPH_ID,
    build_echo_graph,
)
from meta_agent.core.orchestration.graphs.feature_impl import (
    FEATURE_IMPL_GRAPH_ID,
    build_feature_impl_graph,
)
from meta_agent.core.orchestration.graphs.git_inspect import (
    GIT_INSPECT_GRAPH_ID,
    build_git_inspect_graph,
)
from meta_agent.core.orchestration.graphs.shell_agent import (
    SHELL_AGENT_GRAPH_ID,
    build_shell_agent_graph,
)
from meta_agent.core.orchestration.graphs.simple_chat import (
    SIMPLE_CHAT_GRAPH_ID,
    build_simple_chat_graph,
)

__all__ = [
    "AUTO_PR_GRAPH_ID",
    "BUG_FIX_GRAPH_ID",
    "BUG_FIX_V2_GRAPH_ID",
    "CODE_REVIEW_GRAPH_ID",
    "ECHO_GRAPH_ID",
    "FEATURE_IMPL_GRAPH_ID",
    "GIT_INSPECT_GRAPH_ID",
    "SHELL_AGENT_GRAPH_ID",
    "SIMPLE_CHAT_GRAPH_ID",
    "build_auto_pr_graph",
    "build_bug_fix_graph",
    "build_bug_fix_v2_graph",
    "build_code_review_graph",
    "build_echo_graph",
    "build_feature_impl_graph",
    "build_git_inspect_graph",
    "build_shell_agent_graph",
    "build_simple_chat_graph",
]
