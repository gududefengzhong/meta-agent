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
* On a failed verify the graph routes back to ``plan`` once
  (``_MAX_REPLAN_ATTEMPTS``) with the prior plan, diff summary and
  verifier output fed back as context. The replan's new patch lands
  as an additional commit on top of the failed one — no reset / squash
  — so the worktree history transparently records both attempts.
  ``output.attempts`` exposes whether a replan happened (``1`` or ``2``).
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
from string import Template

from meta_agent.core.orchestration.budget_gate import check_budget_policy
from meta_agent.core.orchestration.deps import GraphDeps
from meta_agent.core.orchestration.graph import Graph, GraphError, NodeResult
from meta_agent.core.orchestration.human_gate import HUMAN_FEEDBACK_KEY, build_human_gate
from meta_agent.core.orchestration.llm_streaming import aggregate_stream_to_response
from meta_agent.core.orchestration.state import END, TaskRunState
from meta_agent.core.orchestration.step_kinds import STEP_EDIT, STEP_PLAN
from meta_agent.core.ports.llm import (
    ChatMessage,
    LLMClient,
    LLMRequest,
    MessageRole,
)
from meta_agent.core.ports.prompt_registry import PromptRegistry

BUG_FIX_GRAPH_ID = "builtin.bug_fix"
BUG_FIX_PLAN_PROMPT_ID = "bug_fix.plan.system"
BUG_FIX_PATCH_PROMPT_ID = "bug_fix.patch.system"

_MAX_FILES = 3
_MAX_FILE_BYTES = 10 * 1024
_MAX_REPLAN_ATTEMPTS = 1
"""Maximum number of times ``verify`` may route back to ``plan``. A
value of ``1`` means the graph runs at most two patch attempts before
moving on to ``push`` (with whatever the final verifier said)."""
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


def _plan_messages(
    system: str,
    issue: str,
    files: dict[str, str],
    prior_attempt: dict[str, str] | None = None,
    human_feedback: str | None = None,
) -> tuple[ChatMessage, ...]:
    parts: list[str] = [f"Issue:\n{issue}", "Current files:"]
    for path, content in files.items():
        parts.append(f"\n--- {path} ---\n{content}")
    if prior_attempt is not None:
        parts.append(
            "\nThe previous attempt failed verification. Read the feedback "
            "below and plan a different fix; do not repeat the same mistake."
        )
        parts.append(f"\nPrevious plan:\n{prior_attempt['plan']}")
        parts.append(f"\nPrevious diff summary:\n{prior_attempt['diff_stat']}")
        parts.append(f"\nVerifier output:\n{prior_attempt['verifier_output']}")
    if human_feedback:
        # Phase γ-C "approve with edits": an operator rejected a prior
        # patch and supplied free-text guidance for the next iteration.
        # Render the feedback distinctly from verifier output so the
        # model can tell apart "the tests failed" from "the human
        # wants this done differently".
        parts.append(
            "\nA human reviewer left the following guidance for the "
            "next iteration. Treat it as authoritative; if it conflicts "
            "with the verifier feedback above, prefer the human guidance."
        )
        parts.append(f"\nHuman feedback:\n{human_feedback}")
    return (
        ChatMessage(role=MessageRole.SYSTEM, content=system),
        ChatMessage(role=MessageRole.USER, content="\n".join(parts)),
    )


def _replan_attempts(state: TaskRunState) -> int:
    """Narrow ``state.data['_replan_attempts']`` to a non-negative int."""

    raw = state.data.get("_replan_attempts", 0)
    return raw if isinstance(raw, int) and raw >= 0 else 0


_PRE_PUSH_GATE_ID = "before_push"


def _route_after_verify(state: TaskRunState) -> str:
    """Conditional edge out of ``verify``: pass → push (or gate), fail → replan / push.

    The router is pure: it only reads the verifier report and the
    replan counter. The counter itself is bumped inside ``plan`` on
    entry, so this function is safe to call any number of times.

    When the task's ``_permission_mode`` is ``approve_before_push``
    *and* the verifier passed, the router redirects to the
    ``human_gate`` node so the operator approves the push step;
    other modes go straight to ``push``.
    """

    report = state.data.get("_verifier_report")
    passed = isinstance(report, dict) and bool(report.get("passed"))
    permission_mode = state.data.get("_permission_mode")
    if passed:
        if permission_mode == "approve_before_push":
            return "human_gate"
        return "push"
    if _replan_attempts(state) >= _MAX_REPLAN_ATTEMPTS:
        return "push"
    return "plan"


def _patch_messages(
    system: str, issue: str, plan: str, files: dict[str, str]
) -> tuple[ChatMessage, ...]:
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
    # Capture deps.prompt_registry lazily — the registry guard fires at
    # node execution time, not build time, so legacy bootstraps that
    # pre-date Phase β+ (no PromptRegistry wired) can still materialise
    # the registry mapping without exploding.

    def _require_prompt_registry() -> PromptRegistry:
        if deps.prompt_registry is None:
            raise GraphError(
                "bug_fix requires deps.prompt_registry; wire a PromptRegistry "
                "through GraphDeps at boot"
            )
        return deps.prompt_registry

    async def plan(state: TaskRunState) -> NodeResult:
        # Phase γ-C: per-task budget enforcement runs *first*. The
        # check is no-op when the task has no policy, no threshold, or
        # is still under budget. ``gate_on_threshold`` returns an
        # ``awaiting_approval`` signal (worker pauses the task);
        # ``abort_on_threshold`` routes to END.
        gate_result = await check_budget_policy(state, llm_usage=deps.llm_usage, this_node="plan")
        if gate_result is not None:
            return gate_result
        issue = _required_str(state, "issue_description")
        targets = _target_files(state)
        workspace = Path(_required_str(state, "_workspace_path"))
        if not workspace.is_dir():
            raise GraphError(f"bug_fix: workspace {workspace!s} does not exist")
        snapshot = _read_snapshot(workspace, targets)
        # Detect a replan: a prior failed verifier report in the
        # scratch space means the verify router sent us back. Bump the
        # counter here (rather than in the router, which must stay
        # pure) and feed the previous attempt's summary into the prompt.
        prior_report = state.data.get("_verifier_report")
        prior_patch = state.data.get("_patch")
        prior_attempt: dict[str, str] | None = None
        attempts_so_far = _replan_attempts(state)
        if (
            isinstance(prior_report, dict)
            and not bool(prior_report.get("passed"))
            and isinstance(prior_patch, dict)
        ):
            prior_attempt = {
                "plan": str(state.data.get("_plan", "")),
                "diff_stat": str(prior_patch.get("diff_stat") or ""),
                "verifier_output": str(prior_report.get("output", "")),
            }
            attempts_so_far += 1
        # Phase γ-C: if we landed here because an operator rejected
        # the prior patch with feedback (the gate routes ``reject``
        # back to plan when configured for "approve with edits"), bump
        # the replan counter so the gate's loop is bounded by the
        # same ``_MAX_REPLAN_ATTEMPTS`` ceiling as verifier-driven
        # replans, and surface the feedback to ``_plan_messages``.
        human_feedback: str | None = None
        if bool(state.data.get("_rejected_with_feedback")):
            raw_feedback = state.data.get(HUMAN_FEEDBACK_KEY)
            human_feedback = (
                raw_feedback if isinstance(raw_feedback, str) and raw_feedback else None
            )
            attempts_so_far += 1
        plan_prompt = await _require_prompt_registry().fetch(
            BUG_FIX_PLAN_PROMPT_ID, tenant_id=state.tenant_id
        )
        response = await aggregate_stream_to_response(
            llm,
            LLMRequest(
                messages=_plan_messages(
                    plan_prompt.content,
                    issue,
                    snapshot,
                    prior_attempt,
                    human_feedback=human_feedback,
                ),
                prompt_id=plan_prompt.prompt_id,
                prompt_version=plan_prompt.version,
                step_kind=STEP_PLAN,
            ),
        )
        # Clear the consumed feedback so subsequent iterations do not
        # double-render it; the next reject cycle writes a fresh value.
        return NodeResult(
            data_update={
                "_plan": response.content,
                "_replan_attempts": attempts_so_far,
                "_rejected_with_feedback": False,
                HUMAN_FEEDBACK_KEY: None,
            }
        )

    async def patch(state: TaskRunState) -> NodeResult:
        issue = _required_str(state, "issue_description")
        plan_text = _required_str(state, "_plan")
        branch = _required_str(state, "_workspace_branch")
        targets = _target_files(state)
        workspace = Path(_required_str(state, "_workspace_path"))
        snapshot = _read_snapshot(workspace, targets)
        patch_prompt = await _require_prompt_registry().fetch(
            BUG_FIX_PATCH_PROMPT_ID, tenant_id=state.tenant_id
        )
        rendered_system = Template(patch_prompt.content).safe_substitute(
            allow_list=", ".join(repr(p) for p in snapshot),
            max_files=_MAX_FILES,
            max_file_bytes=_MAX_FILE_BYTES,
        )
        response = await aggregate_stream_to_response(
            llm,
            LLMRequest(
                messages=_patch_messages(rendered_system, issue, plan_text, snapshot),
                prompt_id=patch_prompt.prompt_id,
                prompt_version=patch_prompt.version,
                step_kind=STEP_EDIT,
            ),
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
        replan_count = _replan_attempts(state)
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
                    # Number of patch attempts the graph made before
                    # giving up or succeeding. ``1`` means the first
                    # verify passed; ``2`` means the verify router
                    # routed back to ``plan`` once.
                    "attempts": replan_count + 1,
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
    # Phase γ-A: a single ``human_gate`` between verify and push. The
    # router only sends the state here when ``permission_mode``
    # requires it; ``auto`` tasks bypass the gate entirely. γ-C makes
    # the gate "approve with edits"-capable: a reject routes back to
    # ``plan`` with the operator's feedback merged into the next
    # iteration's prompt (the plan node bumps ``_replan_attempts`` so
    # the existing ``_MAX_REPLAN_ATTEMPTS`` ceiling still bounds the
    # loop).
    g.add_node(
        "human_gate",
        build_human_gate(
            gate_id=_PRE_PUSH_GATE_ID,
            next_node_when_approved="push",
            next_node_when_rejected="plan",
        ),
    )
    g.add_node("push", push)
    g.add_node("finalize", finalize)
    g.set_entry("plan")
    g.add_edge("plan", "patch")
    g.add_edge("patch", "verify")
    # ``verify`` is the only conditional edge: pass → push (or gate
    # if ``permission_mode=approve_before_push``), fail → back to
    # plan up to ``_MAX_REPLAN_ATTEMPTS`` times, then push.
    g.add_conditional("verify", _route_after_verify)
    # The gate node always overrides its next_node at runtime (push
    # on approve, END on reject). The declared edge is the compile-
    # time fallback, never reached at runtime.
    g.add_edge("human_gate", "push")
    g.add_edge("push", "finalize")
    g.add_edge("finalize", END)
    g.compile()
    return g


def _push_skip(reason: str) -> dict[str, object]:
    return {"pushed": False, "skip_reason": reason}
