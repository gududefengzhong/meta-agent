"""Drive a meta-agent graph against a prepared SWE-bench workspace.

Bypasses the worker entirely: builds a minimal GraphDeps + a
local-tool registry pointed at the eval-prepared workspace, then
runs ``builtin.shell_agent`` directly. The worker's auto-managed
workspace lifecycle is the wrong shape for eval (we want to
clone + checkout + apply test_patch ourselves, then point the
agent at the result), so direct invocation is the cleanest
bridge.

What we deliberately don't reuse from the worker
=================================================
* WorkspaceManager — eval owns the workspace lifecycle
* Audit / metering / rate-limit / budget / breaker decorators —
  eval runs against ourselves; production safety wrappers add
  noise without protecting anything real
* OutboxDispatcher / TaskRepository / queue plumbing — single
  shot per call; no persistence story

What we do reuse
================
* ``build_shell_agent_graph`` — same loop production runs
* Local tool implementations + ``register_local_workspace_tools``
* ``aggregate_stream_to_response`` (via the graph itself) so the
  streaming path that ships in production is the path eval
  exercises
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from eval.swebench.instances import SWEBenchInstance
from eval.swebench.patches import extract_patch
from meta_agent.core.capabilities.executor import ToolExecutor
from meta_agent.core.capabilities.registry import ToolRegistry
from meta_agent.core.orchestration.deps import GraphDeps
from meta_agent.core.orchestration.graphs.shell_agent import (
    SHELL_AGENT_GRAPH_ID,
    build_shell_agent_graph,
)
from meta_agent.core.orchestration.state import TaskRunState
from meta_agent.core.ports.llm import LLMClient
from meta_agent.infra.tools import (
    LocalWorkspaceEditTool,
    LocalWorkspaceFileSystemTool,
    LocalWorkspaceShellTool,
    LocalWorkspaceTestTool,
    register_local_workspace_tools,
)

_DEFAULT_TENANT = "eval-swebench"
_DEFAULT_TRACE_PREFIX = "eval-swebench"
_DEFAULT_MAX_STEPS = 20
"""Higher than production's 8 — SWE-bench instances often need
several read / edit / re-read cycles to land a fix. Caller can
still override via ``state_overrides``."""

_PROMPT_TEMPLATE = (
    "Repository: {repo} at commit {commit}\n\n"
    "Issue:\n{issue}\n\n"
    "Investigate the codebase, then make the minimal change "
    "required to resolve the issue. Use the available tools to "
    "read, search, edit and run tests. When you believe the fix "
    "is complete, reply with a short summary of what you changed."
)
"""The user-prompt the agent sees. Kept as a module constant so
PROMPT_VERSION below is a stable hash — bumps automatically when
this string changes, so reports can be filtered by which prompt
revision produced them (EVAL_BASELINE Standard 2).
"""

PROMPT_VERSION = hashlib.sha256(_PROMPT_TEMPLATE.encode("utf-8")).hexdigest()[:12]
"""Short content hash of :data:`_PROMPT_TEMPLATE`.

Use this as the ``prompt_version`` on agent-produced
:class:`InstanceResult` records so two reports are comparable
only when their prompt template was byte-identical. Changing the
template auto-bumps this value.
"""


@dataclass(frozen=True)
class AgentRunResult:
    """What the eval gets back from one agent invocation.

    Both fields are populated regardless of whether the agent
    "succeeded" — eval scoring is decided downstream by
    :func:`eval.swebench.evaluate.evaluate_patch`.
    """

    patch: str
    """Diff of workspace vs ``instance.base_commit``; empty if the
    agent made no on-disk changes."""

    assistant_message: str
    """Final assistant content from the graph. Useful for triage
    (the model often explains why it gave up). May be empty if the
    loop hit ``max_steps`` mid-tool-call."""

    steps: int
    """Number of plan iterations the agent ran. ``< max_steps``
    means it finished cleanly; ``== max_steps`` means it ran out."""


async def run_agent_on_instance(
    instance: SWEBenchInstance,
    workspace: Path | str,
    llm: LLMClient,
    *,
    max_steps: int = _DEFAULT_MAX_STEPS,
    state_overrides: dict[str, object] | None = None,
    base_ref: str | None = None,
) -> AgentRunResult:
    """Run ``builtin.shell_agent`` against the prepared workspace.

    The workspace must already be cloned + checked out at
    ``instance.base_commit`` (use :func:`prepare_workspace`); the
    test_patch may or may not be applied depending on what eval
    flow the caller is running.

    ``base_ref`` is the SHA returned by
    :func:`apply_test_patch` — the diff captured at the end uses
    it as the comparison base so test_patch additions don't end
    up in the agent's extracted patch. Defaults to
    ``instance.base_commit`` for callers that haven't applied
    test_patch (eg. instances with empty test_patch).

    ``state_overrides`` lets callers tweak the seed state — useful
    for tests injecting a smaller ``max_steps`` or extra
    ``tool_names`` allow-list. Reserved keys (``user_prompt``,
    ``_workspace_path``, ``max_steps``) are overridden last and
    win.
    """

    workspace_path = Path(workspace).resolve()
    if not workspace_path.is_dir():
        raise ValueError(f"workspace not found or not a directory: {workspace_path}")

    registry, executor = _build_tool_stack()
    deps = GraphDeps(
        llm=llm,
        tool_registry=registry,
        tool_executor=executor,
    )
    graph = build_shell_agent_graph(deps)

    seed: dict[str, object] = dict(state_overrides or {})
    seed["user_prompt"] = _format_prompt(instance)
    seed["_workspace_path"] = str(workspace_path)
    seed["max_steps"] = max_steps

    state = TaskRunState(
        task_id=f"{_DEFAULT_TRACE_PREFIX}-{instance.instance_id}",
        tenant_id=_DEFAULT_TENANT,
        trace_id=f"{_DEFAULT_TRACE_PREFIX}-{instance.instance_id}",
        graph_id=SHELL_AGENT_GRAPH_ID,
        data=seed,
    )
    final = await graph.run(state)
    output = final.data.get("output") if isinstance(final.data, dict) else None
    assistant_message = ""
    steps_completed = 0
    if isinstance(output, dict):
        am = output.get("assistant_message")
        if isinstance(am, str):
            assistant_message = am
        s = output.get("steps")
        if isinstance(s, int):
            steps_completed = s
    patch = extract_patch(workspace_path, base_ref or instance.base_commit)
    return AgentRunResult(
        patch=patch,
        assistant_message=assistant_message,
        steps=steps_completed,
    )


def _build_tool_stack() -> tuple[ToolRegistry, ToolExecutor]:
    """Construct a local-workspace tool registry the agent will use.

    All paths land in the workspace dir via the ``ToolContext``
    the graph builds from ``state.data["_workspace_path"]``; these
    tool instances are workspace-agnostic.
    """

    registry = ToolRegistry()
    register_local_workspace_tools(
        registry,
        fs=LocalWorkspaceFileSystemTool(),
        edit=LocalWorkspaceEditTool(),
        shell=LocalWorkspaceShellTool(),
        test=LocalWorkspaceTestTool(),
    )
    return registry, ToolExecutor(registry)


def _format_prompt(instance: SWEBenchInstance) -> str:
    """Turn the SWE-bench instance into the user-prompt the agent sees.

    Includes the repo + base_commit context up front so the model
    has it in the conversation, even though the workspace IS that
    state — operators reading the trajectory shouldn't have to
    cross-reference the instance id.
    """

    return _PROMPT_TEMPLATE.format(
        repo=instance.repo,
        commit=instance.base_commit[:12],
        issue=instance.problem_statement,
    )


__all__ = ["PROMPT_VERSION", "AgentRunResult", "run_agent_on_instance"]
