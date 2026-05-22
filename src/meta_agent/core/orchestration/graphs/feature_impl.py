"""Built-in ``feature_impl`` graph: shell_agent under a feature-implementation framing.

Topology and node behavior are identical to ``builtin.shell_agent``;
this module exists so that ``TaskType.FEATURE_IMPL`` resolves to a
distinct graph identity in audit, registry, and ``llm_usage_logs``
records, and so that the task family carries a default system prompt
tuned for natural-language feature requirements rather than the bare
loop framing.

State contract (``state.data``):

* ``user_prompt`` (required): natural-language feature request.
* ``system_prompt`` (optional): caller-supplied framing. When absent
  or empty, :data:`DEFAULT_FEATURE_IMPL_SYSTEM_PROMPT` is injected.
* All other shell_agent state keys (``model``, ``max_steps``,
  ``max_total_tokens``, ``tool_names``, ``target_files`` hint,
  ``verify_suites`` hint) flow through unchanged.

Phase β+ scope: this graph deliberately does *not* commit or push the
resulting diff (unlike ``bug_fix_v2``). Persisting / surfacing the
produced changes is the caller's responsibility for now; a follow-up
PR will introduce the feature_impl → auto_pr chain once retrieval and
prompt-asset infrastructure stabilize.
"""

from __future__ import annotations

from meta_agent.core.orchestration.deps import GraphDeps
from meta_agent.core.orchestration.graph import Graph
from meta_agent.core.orchestration.graphs.shell_agent import build_shell_agent_graph

FEATURE_IMPL_GRAPH_ID = "builtin.feature_impl"

DEFAULT_FEATURE_IMPL_SYSTEM_PROMPT = """\
You are an autonomous feature-implementation agent operating inside a
per-task git worktree. You receive a natural-language feature request
and must drive it to completion using the provided tools.

Operating loop:
1. Read the relevant code. Use fs_list_dir / fs_read / fs_grep before
   proposing edits; do not invent file paths or APIs.
2. Plan a minimal change. Prefer the smallest set of files that
   satisfies the request; do not refactor unrelated code.
3. Apply edits with edit_write or edit_patch_apply. Each edit must
   keep the codebase in a runnable state.
4. Run the appropriate verifier suite via test_run (lint, type-check,
   tests). Treat verifier failures as feedback: read the output,
   adjust, and re-run.
5. Stop when the verifier passes and the request is satisfied. Reply
   with a short summary of what you changed and why.

Constraints:
- Do not call git, network, or shell commands beyond the provided
  tools. The shell_run tool's allow-list is intentionally narrow.
- Do not fabricate test results. Only claim a suite passed after you
  saw test_run return is_error=False for it.
- If the request is ambiguous or the codebase blocks implementation,
  stop and explain rather than guessing.\
"""


def build_feature_impl_graph(deps: GraphDeps) -> Graph:
    """Return a fresh, compiled feature_impl graph bound to ``deps``.

    Delegates to :func:`build_shell_agent_graph` with a feature-impl
    graph id and the default framing prompt. ``deps.tool_registry`` and
    ``deps.tool_executor`` are mandatory; absence raises
    :class:`GraphError` from the underlying builder.
    """

    return build_shell_agent_graph(
        deps,
        graph_id=FEATURE_IMPL_GRAPH_ID,
        default_system_prompt=DEFAULT_FEATURE_IMPL_SYSTEM_PROMPT,
    )
