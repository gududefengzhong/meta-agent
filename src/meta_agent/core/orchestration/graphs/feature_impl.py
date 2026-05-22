"""Built-in ``feature_impl`` graph: shell_agent under a feature-implementation framing.

Topology and node behavior are identical to ``builtin.shell_agent``;
this module exists so that ``TaskType.FEATURE_IMPL`` resolves to a
distinct graph identity in audit, registry, and ``llm_usage_logs``
records, and so that the task family carries a default system prompt
tuned for natural-language feature requirements rather than the bare
loop framing.

Phase β+ PR 2 update: the default system prompt now lives in the
versioned :class:`PromptRegistry` under
:data:`FEATURE_IMPL_SYSTEM_PROMPT_ID`, not as a Python string literal
in this module. The graph still injects it whenever
``state.data['system_prompt']`` is empty — the resolution happens at
plan time via ``deps.prompt_registry`` and the resulting
``(prompt_id, version)`` is attached to every outgoing
:class:`LLMRequest`.

State contract (``state.data``):

* ``user_prompt`` (required): natural-language feature request.
* ``system_prompt`` (optional): caller-supplied raw framing. When
  present this wins over the registry; the LLM call records no
  ``prompt_id`` because provenance belongs to the caller.
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
FEATURE_IMPL_SYSTEM_PROMPT_ID = "feature_impl.system"


def build_feature_impl_graph(deps: GraphDeps) -> Graph:
    """Return a fresh, compiled feature_impl graph bound to ``deps``.

    Delegates to :func:`build_shell_agent_graph` with a feature-impl
    graph id and a registry-backed default system prompt id.
    ``deps.tool_registry``, ``deps.tool_executor`` and
    ``deps.prompt_registry`` are all mandatory; absence raises
    :class:`GraphError` from the underlying builder.
    """

    return build_shell_agent_graph(
        deps,
        graph_id=FEATURE_IMPL_GRAPH_ID,
        default_system_prompt_id=FEATURE_IMPL_SYSTEM_PROMPT_ID,
    )
