"""Built-in git-inspection graph: a workspace smoke-test flow.

Two-node graph that proves the workspace plumbing end-to-end. The
``inspect`` node runs ``git log --oneline -5`` inside the worktree the
:class:`WorkerLoop` provisioned for the task and stores the output;
``summarise`` packages the result. The graph never writes to the
worktree, so a failure here cannot pollute the feature branch.

The subprocess call is intentionally inline rather than routed through
a port: this is a demonstrator, not a business-critical capability.
When real code-touching graphs land (BUG_FIX / AUTO_PR), git access
should move behind a proper ``GitInspector`` port wired through
:class:`GraphDeps` so the graph stays free of process management.
"""

from __future__ import annotations

import asyncio

from meta_agent.core.orchestration.graph import Graph, GraphError, NodeResult
from meta_agent.core.orchestration.state import END, TaskRunState

GIT_INSPECT_GRAPH_ID = "builtin.git_inspect"

_LOG_TIMEOUT_SECONDS = 30.0


def _workspace_path(state: TaskRunState) -> str:
    raw = state.data.get("_workspace_path")
    if not isinstance(raw, str) or not raw:
        raise GraphError(
            "git_inspect: _workspace_path missing from state.data; "
            "the worker did not provision a workspace for this run"
        )
    return raw


async def _inspect(state: TaskRunState) -> NodeResult:
    """Run ``git log --oneline -5`` inside the provisioned worktree."""

    cwd = _workspace_path(state)
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        cwd,
        "log",
        "--oneline",
        "-5",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=_LOG_TIMEOUT_SECONDS
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise GraphError(f"git_inspect: git log timed out after {_LOG_TIMEOUT_SECONDS}s") from None
    if proc.returncode != 0:
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        raise GraphError(f"git_inspect: git log failed (exit={proc.returncode}): {stderr}")
    text = stdout_bytes.decode("utf-8", errors="replace").strip()
    commits = [line for line in text.splitlines() if line]
    return NodeResult(data_update={"_git_log": commits})


async def _summarise(state: TaskRunState) -> NodeResult:
    raw = state.data.get("_git_log", [])
    commits = [str(item) for item in raw] if isinstance(raw, list) else []
    branch = state.data.get("_workspace_branch")
    return NodeResult(
        data_update={
            "output": {
                "branch": branch if isinstance(branch, str) else None,
                "commit_count": len(commits),
                "head": commits[0] if commits else None,
                "log": commits,
            }
        }
    )


def build_git_inspect_graph() -> Graph:
    """Return a fresh, compiled instance of the git-inspect graph."""

    g = Graph(GIT_INSPECT_GRAPH_ID)
    g.add_node("inspect", _inspect)
    g.add_node("summarise", _summarise)
    g.set_entry("inspect")
    g.add_edge("inspect", "summarise")
    g.add_edge("summarise", END)
    g.compile()
    return g
