"""Built-in BUG_FIX v2 graph: minimal tool-use bug-fix loop.

Unlike ``builtin.bug_fix``, which asks the LLM for whole-file
replacements, v2 delegates the edit mechanics to the generic
``shell_agent`` loop and its tool surface.

Current scope:

* Runs on the Phase β tool stack (`fs_*`, `edit_*`, `shell_run`,
  `test_run`) and works with both ``local_git`` and ``docker``
  workspace backends.
* Verification is deterministic via ``test_run``. The default suite is
  ``python_test`` so bug-fix success is gated by tests rather than
  lint alone; callers may override with suites such as
  ``python_lint``, ``typescript_typecheck`` or ``typescript_test``.
* A failed verify triggers at most one replan. The worktree is *not*
  reset between attempts, matching the transparent-history semantics of
  the existing v1 graph.
* On a successful verify the graph stages and commits the workspace
  diff locally. Push stays best-effort: no remote or no token yields a
  local-only commit with ``push_skip_reason`` explaining why.
* When the worker is bootstrapped with tool capabilities, ``BUG_FIX``
  resolves to this graph by default; legacy bootstraps without tools
  still fall back to v1.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from string import Template

from meta_agent.core.capabilities.executor import ToolExecutor
from meta_agent.core.orchestration.deps import GraphDeps
from meta_agent.core.orchestration.graph import Graph, GraphError, NodeResult
from meta_agent.core.orchestration.graphs.shell_agent import (
    SHELL_AGENT_GRAPH_ID,
    build_shell_agent_graph,
)
from meta_agent.core.orchestration.human_gate import HUMAN_FEEDBACK_KEY
from meta_agent.core.orchestration.state import END, TaskRunState
from meta_agent.core.ports.prompt_registry import PromptRegistry
from meta_agent.core.ports.tools import ToolCall, ToolContext

BUG_FIX_V2_GRAPH_ID = "builtin.bug_fix_v2"
BUG_FIX_V2_SYSTEM_PROMPT_ID = "bug_fix_v2.system"

_MAX_REPLAN_ATTEMPTS = 1
_DEFAULT_MAX_STEPS = 8
_GIT_TIMEOUT_SECONDS = 30.0
_GIT_PUSH_TIMEOUT_SECONDS = 120.0
_PUSH_TOKEN_ENV = "AGENT_GIT_PUSH_TOKEN"

_TOOL_FS_READ = "fs_read"
_TOOL_FS_LIST_DIR = "fs_list_dir"
_TOOL_FS_GREP = "fs_grep"
_TOOL_EDIT_WRITE = "edit_write"
_TOOL_EDIT_PATCH_APPLY = "edit_patch_apply"
_TOOL_SHELL_RUN = "shell_run"
_TOOL_TEST_RUN = "test_run"
_DEFAULT_VERIFY_SUITE = "python_test"
_DEFAULT_TOOL_NAMES = [
    _TOOL_FS_READ,
    _TOOL_FS_LIST_DIR,
    _TOOL_FS_GREP,
    _TOOL_EDIT_WRITE,
    _TOOL_EDIT_PATCH_APPLY,
    _TOOL_SHELL_RUN,
    _TOOL_TEST_RUN,
]
_CREDENTIAL_URL_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]*@")


def _redact_credentials(text: str) -> str:
    return _CREDENTIAL_URL_RE.sub(r"\g<scheme><redacted>@", text)


def _required_str(state: TaskRunState, key: str) -> str:
    raw = state.data.get(key)
    if not isinstance(raw, str) or not raw:
        raise GraphError(f"bug_fix_v2: state.data[{key!r}] must be a non-empty str")
    return raw


def _optional_str(state: TaskRunState, key: str) -> str | None:
    raw = state.data.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise GraphError(f"bug_fix_v2: state.data[{key!r}] must be a str or null")
    return raw or None


def _target_files(state: TaskRunState) -> list[str]:
    raw = state.data.get("target_files")
    if (
        not isinstance(raw, list)
        or not raw
        or not all(isinstance(item, str) and item for item in raw)
    ):
        raise GraphError("bug_fix_v2: state.data['target_files'] must be a non-empty list[str]")
    return list(raw)


def _read_snapshot(workspace: Path, files: list[str]) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for rel in files:
        path = workspace / rel
        if path.exists():
            if not path.is_file():
                raise GraphError(f"bug_fix_v2: target_files entry {rel!r} is not a regular file")
            snapshot[rel] = path.read_text(encoding="utf-8")
        else:
            snapshot[rel] = ""
    return snapshot


def _replan_attempts(state: TaskRunState) -> int:
    raw = state.data.get("_replan_attempts", 0)
    return raw if isinstance(raw, int) and raw >= 0 else 0


def _route_after_verify(state: TaskRunState) -> str:
    raw_report = state.data.get("_verify")
    passed = isinstance(raw_report, dict) and bool(raw_report.get("passed"))
    if passed or _replan_attempts(state) >= _MAX_REPLAN_ATTEMPTS:
        return "push"
    return "prepare"


def _render_system_prompt(template: str, targets: list[str]) -> str:
    """Apply the ``$allow_list`` placeholder of the seed template.

    Uses :class:`string.Template` rather than ``str.format`` so seed
    text containing literal ``{`` / ``}`` (JSON schema snippets,
    f-string-looking braces) does not need escaping in the registry.
    """

    listing = ", ".join(repr(path) for path in targets)
    return Template(template).safe_substitute(allow_list=listing)


def _user_prompt(
    *,
    issue: str,
    targets: list[str],
    snapshot: dict[str, str],
    prior_verifier_output: str | None,
    prior_diff_stat: str | None,
    human_feedback: str | None = None,
) -> str:
    parts: list[str] = [f"Issue:\n{issue}", "\nAllowed files:"]
    for rel in targets:
        content = snapshot.get(rel, "")
        parts.append(f"\n--- {rel} ---\n{content}")
    if prior_verifier_output:
        parts.append(
            "\nThe previous attempt failed verification. Read the feedback below, "
            "adjust the edit, and avoid repeating the same mistake."
        )
        parts.append(f"\nVerifier output:\n{prior_verifier_output}")
        if prior_diff_stat:
            parts.append(f"\nPrevious diff stat:\n{prior_diff_stat}")
    if human_feedback:
        # Phase γ-C "approve with edits": forward operator-supplied
        # free-text guidance into the next iteration. Distinct from
        # the verifier section so the model can tell apart machine
        # failure feedback from human authorial intent.
        parts.append(
            "\nA human reviewer left the following guidance. Treat it "
            "as authoritative; if it conflicts with the verifier "
            "feedback above, prefer the human guidance."
        )
        parts.append(f"\nHuman feedback:\n{human_feedback}")
    return "\n".join(parts)


async def _run_subprocess(args: list[str], cwd: Path, *, timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise GraphError(f"bug_fix_v2: {args[0]} timed out after {timeout}s") from None
    return (
        proc.returncode or 0,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


async def _git(args: list[str], cwd: Path) -> str:
    code, stdout, stderr = await _run_subprocess(
        ["git", "-C", str(cwd), *args], cwd, timeout=_GIT_TIMEOUT_SECONDS
    )
    if code != 0:
        raise GraphError(f"bug_fix_v2: git {args[0]} failed (exit={code}): {stderr.strip()}")
    return stdout


async def _diff_stat(workspace: Path) -> str:
    _code, stdout, _stderr = await _run_subprocess(
        ["git", "diff", "--stat"], workspace, timeout=30.0
    )
    return stdout.strip()


async def _diff_patch(workspace: Path) -> str:
    _code, stdout, _stderr = await _run_subprocess(["git", "diff"], workspace, timeout=30.0)
    return stdout


def _verify_suite(state: TaskRunState) -> str:
    raw = state.data.get("verify_suite")
    if raw is None:
        return _DEFAULT_VERIFY_SUITE
    if not isinstance(raw, str) or not raw:
        raise GraphError("bug_fix_v2: state.data['verify_suite'] must be a non-empty str")
    return raw


async def _verify_with_test_tool(
    state: TaskRunState,
    changed: list[str],
    *,
    workspace: Path,
    tool_executor: ToolExecutor,
) -> dict[str, object]:
    if not changed:
        return {
            "passed": False,
            "output": "no files changed; agent produced no workspace diff",
        }
    suite = _verify_suite(state)
    cap = state.data.get("output_byte_cap", 65536)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap <= 0:
        raise GraphError("bug_fix_v2: output_byte_cap must be a positive int")
    result = await tool_executor.execute(
        ToolCall(
            id=f"verify-{state.task_id}",
            name=_TOOL_TEST_RUN,
            arguments={"suite": suite, "targets": list(changed)},
        ),
        ToolContext(
            tenant_id=state.tenant_id,
            task_id=state.task_id,
            trace_id=state.trace_id,
            workspace_path=workspace,
            output_byte_cap=cap,
        ),
    )
    return {"passed": not result.is_error, "output": result.content, "suite": suite}


def _files_changed(baseline: dict[str, str], current: dict[str, str]) -> list[str]:
    changed: list[str] = []
    for path, before in baseline.items():
        if current.get(path, "") != before:
            changed.append(path)
    return changed


async def _commit_workspace(
    workspace: Path,
    *,
    issue: str,
    branch: str,
    files_changed: list[str],
) -> tuple[str | None, str, list[str]]:
    if not files_changed:
        return None, "", []
    await _git(["add", "--", *files_changed], workspace)
    staged = await _git(["diff", "--cached", "--name-only"], workspace)
    committed = [line for line in staged.splitlines() if line]
    if not committed:
        return None, "", []
    first_line = issue.splitlines()[0] if issue.strip() else "bug fix"
    title = first_line[:72]
    message = f"agent({branch}): {title}"
    await _git(
        [
            "-c",
            "user.email=agent@meta-agent.local",
            "-c",
            "user.name=meta-agent",
            "commit",
            "-m",
            message,
        ],
        workspace,
    )
    sha = (await _git(["rev-parse", "HEAD"], workspace)).strip() or None
    diff_stat = (await _git(["show", "--stat", "--format=", "HEAD"], workspace)).strip()
    return sha, diff_stat, committed


async def _git_push(workspace: Path, branch: str, *, token: str) -> None:
    helper = f'!f() {{ echo username=x-access-token; echo "password=${_PUSH_TOKEN_ENV}"; }}; f'
    args = [
        "git",
        "-C",
        str(workspace),
        "-c",
        f"credential.helper={helper}",
        "push",
        "origin",
        f"{branch}:{branch}",
    ]
    env = {**os.environ, _PUSH_TOKEN_ENV: token}
    proc = await asyncio.create_subprocess_exec(
        *args,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=_GIT_PUSH_TIMEOUT_SECONDS
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise GraphError(
            f"bug_fix_v2: git push timed out after {_GIT_PUSH_TIMEOUT_SECONDS}s"
        ) from None
    if proc.returncode != 0:
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        detail = _redact_credentials(stderr or stdout or "no output")
        raise GraphError(f"bug_fix_v2: git push failed (exit={proc.returncode}): {detail}")


def _push_skip(
    reason: str,
    *,
    commit_sha: str | None,
    diff_stat: str,
    files_changed: list[str],
) -> dict[str, object]:
    return {
        "commit_sha": commit_sha,
        "diff_stat": diff_stat,
        "files_changed": files_changed,
        "pushed": False,
        "skip_reason": reason,
    }


def build_bug_fix_v2_graph(deps: GraphDeps) -> Graph:
    """Build the compiled :data:`BUG_FIX_V2_GRAPH_ID` graph."""

    inner_shell_graph = build_shell_agent_graph(deps)
    push_token: str | None = deps.git_push_token
    if deps.tool_executor is None:
        raise GraphError("bug_fix_v2 requires deps.tool_executor")
    tool_executor = deps.tool_executor

    def _require_prompt_registry() -> PromptRegistry:
        if deps.prompt_registry is None:
            raise GraphError(
                "bug_fix_v2 requires deps.prompt_registry; wire a PromptRegistry "
                "through GraphDeps at boot"
            )
        return deps.prompt_registry

    async def prepare(state: TaskRunState) -> NodeResult:
        issue = _required_str(state, "issue_description")
        workspace = Path(_required_str(state, "_workspace_path"))
        targets = _target_files(state)
        if not workspace.is_dir():
            raise GraphError(f"bug_fix_v2: workspace {workspace!s} does not exist")
        snapshot = _read_snapshot(workspace, targets)
        baseline_raw = state.data.get("_baseline_snapshot")
        baseline = baseline_raw if isinstance(baseline_raw, dict) else snapshot
        prior_report = state.data.get("_verify")
        prior_verifier_output = None
        prior_diff_stat = None
        attempts = _replan_attempts(state)
        if isinstance(prior_report, dict) and not bool(prior_report.get("passed")):
            attempts += 1
            raw_output = prior_report.get("output")
            prior_verifier_output = raw_output if isinstance(raw_output, str) else None
            raw_diff = state.data.get("_diff_stat")
            prior_diff_stat = raw_diff if isinstance(raw_diff, str) else None
        # Phase γ-C: consume operator feedback from a prior reject if
        # this graph ever grows a human_gate (v1 already does; v2's
        # gate lands in a follow-up). Reading the slot here means the
        # prompt builder is ready the moment the gate is wired.
        human_feedback: str | None = None
        if bool(state.data.get("_rejected_with_feedback")):
            raw_feedback = state.data.get(HUMAN_FEEDBACK_KEY)
            human_feedback = (
                raw_feedback if isinstance(raw_feedback, str) and raw_feedback else None
            )
            attempts += 1
        tool_names_raw = state.data.get("tool_names")
        tool_names = (
            tool_names_raw
            if isinstance(tool_names_raw, list)
            and all(isinstance(name, str) for name in tool_names_raw)
            else _DEFAULT_TOOL_NAMES
        )
        prompt_asset = await _require_prompt_registry().fetch(
            BUG_FIX_V2_SYSTEM_PROMPT_ID, tenant_id=state.tenant_id
        )
        inner_state = TaskRunState(
            task_id=state.task_id,
            tenant_id=state.tenant_id,
            trace_id=state.trace_id,
            graph_id=SHELL_AGENT_GRAPH_ID,
            data={
                "system_prompt": _render_system_prompt(prompt_asset.content, targets),
                "system_prompt_id": prompt_asset.prompt_id,
                "system_prompt_version": prompt_asset.version,
                "user_prompt": _user_prompt(
                    issue=issue,
                    targets=targets,
                    snapshot=snapshot,
                    prior_verifier_output=prior_verifier_output,
                    prior_diff_stat=prior_diff_stat,
                    human_feedback=human_feedback,
                ),
                "tool_names": list(tool_names),
                "max_steps": state.data.get("max_steps", _DEFAULT_MAX_STEPS),
                "max_total_tokens": state.data.get("max_total_tokens"),
                "output_byte_cap": state.data.get("output_byte_cap", 65536),
                "_workspace_path": str(workspace),
                "model": state.data.get("model"),
                "temperature": state.data.get("temperature"),
                "max_tokens": state.data.get("max_tokens"),
            },
        )
        return NodeResult(
            data_update={
                "_baseline_snapshot": baseline,
                "_replan_attempts": attempts,
                "_inner_state": inner_state.model_dump(mode="json"),
                # Clear feedback once consumed so a subsequent reject
                # cycle starts from a clean slot.
                "_rejected_with_feedback": False,
                HUMAN_FEEDBACK_KEY: None,
            }
        )

    async def agent(state: TaskRunState) -> NodeResult:
        raw = state.data.get("_inner_state")
        if not isinstance(raw, dict):
            raise GraphError("bug_fix_v2: prepare did not emit _inner_state")
        inner_state = TaskRunState.model_validate(raw)
        final = await inner_shell_graph.run(inner_state)
        output = final.data.get("output")
        if not isinstance(output, dict):
            raise GraphError("bug_fix_v2: inner shell_agent did not emit output")
        return NodeResult(
            data_update={
                "_agent_output": output,
                "_agent_state": final.model_dump(mode="json"),
            }
        )

    async def verify(state: TaskRunState) -> NodeResult:
        workspace = Path(_required_str(state, "_workspace_path"))
        targets = _target_files(state)
        baseline_raw = state.data.get("_baseline_snapshot")
        if not isinstance(baseline_raw, dict):
            raise GraphError("bug_fix_v2: missing baseline snapshot")
        baseline = {str(k): str(v) for k, v in baseline_raw.items()}
        current = _read_snapshot(workspace, targets)
        changed = _files_changed(baseline, current)
        report = await _verify_with_test_tool(
            state,
            changed,
            workspace=workspace,
            tool_executor=tool_executor,
        )
        return NodeResult(
            data_update={
                "_files_changed": changed,
                "_verify": report,
                "_diff_stat": await _diff_stat(workspace),
                "_patch": await _diff_patch(workspace),
            }
        )

    async def push(state: TaskRunState) -> NodeResult:
        raw_verify = state.data.get("_verify")
        raw_changed = state.data.get("_files_changed")
        if not isinstance(raw_verify, dict):
            raise GraphError("bug_fix_v2: push reached without verifier report")
        files_changed = [str(item) for item in raw_changed] if isinstance(raw_changed, list) else []
        if not files_changed:
            return NodeResult(
                data_update={
                    "_push": _push_skip(
                        "no_commit", commit_sha=None, diff_stat="", files_changed=[]
                    )
                }
            )
        if not bool(raw_verify.get("passed")):
            return NodeResult(
                data_update={
                    "_push": _push_skip(
                        "verifier_failed",
                        commit_sha=None,
                        diff_stat=str(state.data.get("_diff_stat", "")),
                        files_changed=files_changed,
                    )
                }
            )
        workspace = Path(_required_str(state, "_workspace_path"))
        branch = _required_str(state, "_workspace_branch")
        issue = _required_str(state, "issue_description")
        commit_sha, diff_stat, committed = await _commit_workspace(
            workspace,
            issue=issue,
            branch=branch,
            files_changed=files_changed,
        )
        if not isinstance(commit_sha, str) or not commit_sha:
            return NodeResult(
                data_update={
                    "_push": _push_skip(
                        "no_commit", commit_sha=None, diff_stat="", files_changed=[]
                    )
                }
            )
        repo_url = state.data.get("repo_url")
        if not isinstance(repo_url, str) or not repo_url:
            return NodeResult(
                data_update={
                    "_push": _push_skip(
                        "no_repo_url",
                        commit_sha=commit_sha,
                        diff_stat=diff_stat,
                        files_changed=committed,
                    )
                }
            )
        if not push_token:
            return NodeResult(
                data_update={
                    "_push": _push_skip(
                        "no_token",
                        commit_sha=commit_sha,
                        diff_stat=diff_stat,
                        files_changed=committed,
                    )
                }
            )
        await _git_push(workspace, branch, token=push_token)
        return NodeResult(
            data_update={
                "_push": {
                    "commit_sha": commit_sha,
                    "diff_stat": diff_stat,
                    "files_changed": committed,
                    "pushed": True,
                    "skip_reason": None,
                }
            }
        )

    async def finalize(state: TaskRunState) -> NodeResult:
        raw_output = state.data.get("_agent_output")
        agent_output = raw_output if isinstance(raw_output, dict) else {}
        raw_verify = state.data.get("_verify")
        verify = raw_verify if isinstance(raw_verify, dict) else {}
        raw_push = state.data.get("_push")
        push = raw_push if isinstance(raw_push, dict) else {}
        raw_changed = push.get("files_changed")
        changed = [str(item) for item in raw_changed] if isinstance(raw_changed, list) else []
        commit_sha = push.get("commit_sha")
        commit_sha_str = commit_sha if isinstance(commit_sha, str) else None
        push_skip_reason = push.get("skip_reason")
        return NodeResult(
            data_update={
                "output": {
                    "assistant_message": str(agent_output.get("assistant_message", "")),
                    "steps": int(agent_output.get("steps", 0) or 0),
                    "tool_invocations": int(agent_output.get("tool_invocations", 0) or 0),
                    "truncated_by_max_steps": bool(
                        agent_output.get("truncated_by_max_steps", False)
                    ),
                    "truncated_by_token_budget": bool(
                        agent_output.get("truncated_by_token_budget", False)
                    ),
                    "usage": agent_output.get("usage", {}),
                    "files_changed": changed,
                    "patch": str(state.data.get("_patch", "")),
                    "diff_stat": str(push.get("diff_stat") or state.data.get("_diff_stat", "")),
                    "verifier_passed": bool(verify.get("passed", False)),
                    "verifier_output": str(verify.get("output", "")),
                    "attempts": _replan_attempts(state) + 1,
                    "branch": _optional_str(state, "_workspace_branch"),
                    "commit_sha": commit_sha_str,
                    "repo_url": _optional_str(state, "repo_url"),
                    "base_ref": _optional_str(state, "base_ref"),
                    "head_branch": _optional_str(state, "_workspace_branch"),
                    "head_commit_sha": commit_sha_str,
                    "pushed": bool(push.get("pushed")),
                    "push_skip_reason": (
                        str(push_skip_reason) if isinstance(push_skip_reason, str) else None
                    ),
                }
            }
        )

    g = Graph(BUG_FIX_V2_GRAPH_ID)
    g.add_node("prepare", prepare)
    g.add_node("agent", agent)
    g.add_node("verify", verify)
    g.add_node("push", push)
    g.add_node("finalize", finalize)
    g.set_entry("prepare")
    g.add_edge("prepare", "agent")
    g.add_edge("agent", "verify")
    g.add_conditional("verify", _route_after_verify)
    g.add_edge("push", "finalize")
    g.add_edge("finalize", END)
    g.compile()
    return g
