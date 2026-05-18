"""Built-in BUG_FIX graph: minimal code-touching business flow.

Five nodes — ``plan`` → ``patch`` → ``verify`` → ``push`` → ``finalize``
— that turn an issue description + a caller-supplied file allow-list
into a committed (and optionally pushed) change on the per-task
worktree provisioned by the worker.

Scope (P1.x first cut):

* The graph never proposes which files to touch; ``target_files`` is
  a strict allow-list supplied by the caller. Patches outside it are
  rejected as a graph error.
* The LLM emits whole-file new contents as JSON, not a unified diff —
  diff parsing is brittle and adds nothing of value at this scale.
* Verification runs ``ruff check`` on the modified files. A ruff
  failure is reported as ``output.verifier_passed=False`` but does
  NOT mark the task as failed; the task succeeds with a patch the
  caller can choose to discard. ``state.error`` fail-fast routing
  for business failures is deferred (see ``simple_chat`` docstring).
* ``push`` is best-effort: it skips when the worktree was provisioned
  without a remote, when verification failed, or when no credentials
  were configured. PR creation lives in the separate ``AUTO_PR`` graph;
  this node only makes the commit reachable on origin.

Hard ceilings (``_MAX_FILES`` / ``_MAX_FILE_BYTES``) bound cost-runaway
risk during the initial rollout. Raise once real telemetry exists.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

from meta_agent.core.orchestration.deps import GraphDeps
from meta_agent.core.orchestration.graph import Graph, GraphError, NodeResult
from meta_agent.core.orchestration.state import END, TaskRunState
from meta_agent.core.ports.llm import (
    ChatMessage,
    LLMClient,
    LLMRequest,
    MessageRole,
)

BUG_FIX_GRAPH_ID = "builtin.bug_fix"

_MAX_FILES = 3
_MAX_FILE_BYTES = 10 * 1024
_GIT_TIMEOUT_SECONDS = 30.0
_RUFF_TIMEOUT_SECONDS = 60.0
_GIT_PUSH_TIMEOUT_SECONDS = 120.0
_PUSH_TOKEN_ENV = "AGENT_GIT_PUSH_TOKEN"
"""Env var used to ferry the push token from this process to the
``git`` subprocess via a one-shot credential helper. Naming it here
keeps the test suite from having to guess; the value itself is never
logged or echoed."""

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)
_CREDENTIAL_URL_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]*@")


def _redact_credentials(text: str) -> str:
    """Strip ``user:pass`` from URLs so error / log surfaces stay safe."""

    return _CREDENTIAL_URL_RE.sub(r"\g<scheme><redacted>@", text)


def _required_str(state: TaskRunState, key: str) -> str:
    raw = state.data.get(key)
    if not isinstance(raw, str) or not raw:
        raise GraphError(f"bug_fix: state.data[{key!r}] must be a non-empty str")
    return raw


def _target_files(state: TaskRunState) -> list[str]:
    raw = state.data.get("target_files")
    if not isinstance(raw, list) or not raw:
        raise GraphError("bug_fix: state.data['target_files'] must be a non-empty list of paths")
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item:
            raise GraphError("bug_fix: 'target_files' entries must be non-empty strings")
        path = Path(item)
        if path.is_absolute() or ".." in path.parts:
            raise GraphError(f"bug_fix: target_files entry {item!r} must be repo-relative")
        out.append(item)
    if len(out) > _MAX_FILES:
        raise GraphError(f"bug_fix: target_files exceeds max_files={_MAX_FILES}")
    return out


def _read_snapshot(workspace: Path, files: list[str]) -> dict[str, str]:
    """Read each allow-listed file; missing files map to ``''``."""

    snapshot: dict[str, str] = {}
    for rel in files:
        path = workspace / rel
        if path.exists():
            if not path.is_file():
                raise GraphError(f"bug_fix: target_files entry {rel!r} is not a regular file")
            snapshot[rel] = path.read_text(encoding="utf-8")
        else:
            snapshot[rel] = ""
    return snapshot


def _strip_fence(text: str) -> str:
    match = _FENCE_RE.match(text.strip())
    return match.group(1) if match else text


async def _run_subprocess(
    args: list[str], cwd: str | Path, *, timeout: float
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise GraphError(f"bug_fix: {args[0]} timed out after {timeout}s") from None
    return (
        proc.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


def _plan_messages(issue: str, files: dict[str, str]) -> tuple[ChatMessage, ...]:
    system = (
        "You are a code repair agent. Read the issue and the listed files, "
        "then write a concise plan (at most 6 lines) describing the minimal "
        "change required to fix the bug. Do not output code yet; only the plan."
    )
    parts: list[str] = [f"Issue:\n{issue}", "Current files:"]
    for path, content in files.items():
        parts.append(f"\n--- {path} ---\n{content}")
    return (
        ChatMessage(role=MessageRole.SYSTEM, content=system),
        ChatMessage(role=MessageRole.USER, content="\n".join(parts)),
    )


def _patch_messages(issue: str, plan: str, files: dict[str, str]) -> tuple[ChatMessage, ...]:
    listing = ", ".join(repr(p) for p in files)
    system = (
        "You are a code patcher. Apply the provided plan to fix the issue. "
        'Return ONLY JSON of the form {"files":[{"path":"<rel>","content":"<full>"}]}. '
        f"You may only modify files in this allow-list: [{listing}]. "
        f"At most {_MAX_FILES} files; each file at most {_MAX_FILE_BYTES} bytes. "
        "Emit the FULL new content of each modified file, not a diff."
    )
    parts: list[str] = [f"Issue:\n{issue}", f"\nPlan:\n{plan}", "\nCurrent files:"]
    for path, content in files.items():
        parts.append(f"\n--- {path} ---\n{content}")
    return (
        ChatMessage(role=MessageRole.SYSTEM, content=system),
        ChatMessage(role=MessageRole.USER, content="\n".join(parts)),
    )


def _parse_patch(raw: str, allow_list: set[str]) -> list[tuple[str, str]]:
    cleaned = _strip_fence(raw)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GraphError(f"bug_fix: patch response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise GraphError("bug_fix: patch response must be a JSON object")
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise GraphError("bug_fix: patch response must contain non-empty 'files' list")
    if len(files) > _MAX_FILES:
        raise GraphError(f"bug_fix: patch response exceeds max_files={_MAX_FILES}")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise GraphError("bug_fix: patch entries must be JSON objects")
        path = entry.get("path")
        content = entry.get("content")
        if not isinstance(path, str) or not path:
            raise GraphError("bug_fix: patch entry missing 'path'")
        if not isinstance(content, str):
            raise GraphError(f"bug_fix: patch entry for {path!r} missing 'content' string")
        if path not in allow_list:
            raise GraphError(f"bug_fix: patch entry {path!r} not in target_files allow-list")
        if path in seen:
            raise GraphError(f"bug_fix: patch entry {path!r} appears more than once")
        if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
            raise GraphError(
                f"bug_fix: patch entry {path!r} exceeds max_file_bytes={_MAX_FILE_BYTES}"
            )
        seen.add(path)
        out.append((path, content))
    return out


async def _git(args: list[str], cwd: Path) -> str:
    code, stdout, stderr = await _run_subprocess(
        ["git", "-C", str(cwd), *args], cwd, timeout=_GIT_TIMEOUT_SECONDS
    )
    if code != 0:
        raise GraphError(f"bug_fix: git {args[0]} failed (exit={code}): {stderr.strip()}")
    return stdout


async def _commit_patch(
    workspace: Path,
    patches: list[tuple[str, str]],
    issue: str,
    branch: str,
) -> tuple[str | None, str, list[str]]:
    """Write patches, stage, commit. Return (sha, diff_stat, files_changed)."""

    for rel, content in patches:
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    await _git(["add", "--", *[rel for rel, _ in patches]], workspace)
    staged = await _git(["diff", "--cached", "--name-only"], workspace)
    files_changed = [line for line in staged.splitlines() if line]
    if not files_changed:
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
    return sha, diff_stat, files_changed


async def _git_push(workspace: Path, branch: str, *, token: str) -> None:
    """Push ``branch`` to ``origin`` using a one-shot credential helper.

    The token value is passed through the subprocess environment, never
    on the command line, so it cannot leak into process tables, audit
    logs or shell history. The helper script is inlined as a ``-c``
    config override so we do not have to write any state to disk.
    """

    helper = (
        '!f() { '
        'echo username=x-access-token; '
        f'echo "password=${_PUSH_TOKEN_ENV}"; '
        '}; f'
    )
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
            f"bug_fix: git push timed out after {_GIT_PUSH_TIMEOUT_SECONDS}s"
        ) from None
    if proc.returncode != 0:
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        detail = _redact_credentials(stderr or stdout or "no output")
        raise GraphError(f"bug_fix: git push failed (exit={proc.returncode}): {detail}")


def build_bug_fix_graph(deps: GraphDeps) -> Graph:
    """Return a fresh, compiled BUG_FIX graph bound to ``deps.llm``."""

    llm: LLMClient = deps.llm
    push_token: str | None = deps.git_push_token

    async def plan(state: TaskRunState) -> NodeResult:
        issue = _required_str(state, "issue_description")
        targets = _target_files(state)
        workspace = Path(_required_str(state, "_workspace_path"))
        if not workspace.is_dir():
            raise GraphError(f"bug_fix: workspace {workspace!s} does not exist")
        snapshot = _read_snapshot(workspace, targets)
        response = await llm.complete(LLMRequest(messages=_plan_messages(issue, snapshot)))
        return NodeResult(data_update={"_plan": response.content})

    async def patch(state: TaskRunState) -> NodeResult:
        issue = _required_str(state, "issue_description")
        plan_text = _required_str(state, "_plan")
        branch = _required_str(state, "_workspace_branch")
        targets = _target_files(state)
        workspace = Path(_required_str(state, "_workspace_path"))
        snapshot = _read_snapshot(workspace, targets)
        response = await llm.complete(
            LLMRequest(messages=_patch_messages(issue, plan_text, snapshot))
        )
        patches = _parse_patch(response.content, allow_list=set(targets))
        sha, diff_stat, files_changed = await _commit_patch(workspace, patches, issue, branch)
        return NodeResult(
            data_update={
                "_patch": {
                    "commit_sha": sha,
                    "diff_stat": diff_stat,
                    "files_changed": files_changed,
                }
            }
        )

    async def verify(state: TaskRunState) -> NodeResult:
        raw_patch = state.data.get("_patch")
        if not isinstance(raw_patch, dict):
            raise GraphError("bug_fix: verify reached without _patch from prior node")
        raw_changed = raw_patch.get("files_changed")
        files_changed = [str(p) for p in raw_changed] if isinstance(raw_changed, list) else []
        if not files_changed:
            return NodeResult(
                data_update={
                    "_verifier_report": {
                        "passed": False,
                        "output": "no files changed; patch produced an empty diff",
                    }
                }
            )
        workspace = _required_str(state, "_workspace_path")
        code, stdout, stderr = await _run_subprocess(
            ["ruff", "check", "--", *files_changed],
            cwd=workspace,
            timeout=_RUFF_TIMEOUT_SECONDS,
        )
        combined = (stdout + stderr).strip()
        return NodeResult(
            data_update={
                "_verifier_report": {
                    "passed": code == 0,
                    "output": "" if code == 0 else combined,
                }
            }
        )

    async def push(state: TaskRunState) -> NodeResult:
        raw_patch = state.data.get("_patch")
        raw_report = state.data.get("_verifier_report")
        if not isinstance(raw_patch, dict) or not isinstance(raw_report, dict):
            raise GraphError("bug_fix: push reached with malformed scratch state")
        repo_url = state.data.get("repo_url")
        commit_sha = raw_patch.get("commit_sha")
        # Skip order is intentional: a missing remote means there is
        # nowhere to push regardless of the other checks; an unverified
        # patch must not reach origin even if a token is present; the
        # token check comes last so the prior signals stay visible in
        # the output for downstream graphs.
        if not isinstance(repo_url, str) or not repo_url:
            return NodeResult(data_update={"_push": _push_skip("no_repo_url")})
        if not isinstance(commit_sha, str) or not commit_sha:
            return NodeResult(data_update={"_push": _push_skip("no_commit")})
        if not bool(raw_report.get("passed")):
            return NodeResult(data_update={"_push": _push_skip("verifier_failed")})
        if not push_token:
            return NodeResult(data_update={"_push": _push_skip("no_token")})
        workspace = Path(_required_str(state, "_workspace_path"))
        branch = _required_str(state, "_workspace_branch")
        await _git_push(workspace, branch, token=push_token)
        return NodeResult(data_update={"_push": {"pushed": True, "skip_reason": None}})

    async def finalize(state: TaskRunState) -> NodeResult:
        raw_patch = state.data.get("_patch")
        raw_report = state.data.get("_verifier_report")
        raw_push = state.data.get("_push")
        if (
            not isinstance(raw_patch, dict)
            or not isinstance(raw_report, dict)
            or not isinstance(raw_push, dict)
        ):
            raise GraphError("bug_fix: finalize reached with malformed scratch state")
        branch = _required_str(state, "_workspace_branch")
        raw_changed = raw_patch.get("files_changed")
        files_changed = [str(p) for p in raw_changed] if isinstance(raw_changed, list) else []
        commit_sha = raw_patch.get("commit_sha")
        commit_sha_str = commit_sha if isinstance(commit_sha, str) else None
        repo_url = state.data.get("repo_url")
        base_ref = state.data.get("base_ref")
        push_skip_reason = raw_push.get("skip_reason")
        return NodeResult(
            data_update={
                "output": {
                    # Legacy fields kept for in-tree consumers that
                    # predate the auto-pr handoff contract.
                    "branch": branch,
                    "commit_sha": commit_sha_str,
                    "files_changed": files_changed,
                    "diff_stat": str(raw_patch.get("diff_stat") or ""),
                    "verifier_passed": bool(raw_report.get("passed")),
                    "verifier_output": str(raw_report.get("output", "")),
                    # Handoff fields consumed by ``builtin.auto_pr``:
                    # exporting them here means a follow-up AUTO_PR task
                    # can be enqueued with this exact output as its
                    # input payload without any field renaming.
                    "repo_url": repo_url if isinstance(repo_url, str) else None,
                    "base_ref": base_ref if isinstance(base_ref, str) else None,
                    "head_branch": branch,
                    "head_commit_sha": commit_sha_str,
                    "pushed": bool(raw_push.get("pushed")),
                    "push_skip_reason": (
                        str(push_skip_reason) if isinstance(push_skip_reason, str) else None
                    ),
                }
            }
        )

    g = Graph(BUG_FIX_GRAPH_ID)
    g.add_node("plan", plan)
    g.add_node("patch", patch)
    g.add_node("verify", verify)
    g.add_node("push", push)
    g.add_node("finalize", finalize)
    g.set_entry("plan")
    g.add_edge("plan", "patch")
    g.add_edge("patch", "verify")
    g.add_edge("verify", "push")
    g.add_edge("push", "finalize")
    g.add_edge("finalize", END)
    g.compile()
    return g


def _push_skip(reason: str) -> dict[str, object]:
    return {"pushed": False, "skip_reason": reason}
