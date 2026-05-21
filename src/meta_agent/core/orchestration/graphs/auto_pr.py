"""Built-in AUTO_PR graph: publish a feature-branch commit as a PR.

Three nodes — ``prepare`` → ``publish`` → ``finalize`` — that turn the
output of an upstream BUG_FIX run (commit on a feature branch in the
worker's worktree) into a structured publish result. The graph never
touches a worktree itself: by the time ``publish`` runs the commit
must already exist on the remote and ``head_commit_sha`` must be set.

Scope (v1):

* Pure orchestration: no LLM, no disk IO, no subprocess. Title and
  body are deterministic templates rendered from the upstream task
  output. LLM-polished PR descriptions are deferred.
* The publish step delegates to :class:`GitProvider`; v1 wires the
  ``fake`` adapter so this milestone can land an end-to-end task
  contract before any real GitHub credentials are required.
* "Scheme X" applies: the graph SUCCEEDS even when it decides to
  skip publishing (no repo / no commit / verifier failed); the
  caller inspects ``output.action`` and ``output.reason``. Only
  contract failures (missing required fields, malformed types,
  provider errors) raise :class:`GraphError`.
* No update / comment / close operations. v1 is open-or-reuse only.

Hard ceilings cap output size so a pathological verifier dump cannot
blow up downstream rendering: ``_MAX_TITLE_CHARS``, ``_MAX_BODY_CHARS``,
``_MAX_VERIFIER_OUTPUT_CHARS``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from meta_agent.core.orchestration.deps import GraphDeps
from meta_agent.core.orchestration.graph import Graph, GraphError, NodeResult
from meta_agent.core.orchestration.state import END, TaskRunState
from meta_agent.core.ports.git_provider import (
    GitProviderError,
    PullRequestRef,
)

AUTO_PR_GRAPH_ID = "builtin.auto_pr"

_MAX_TITLE_CHARS = 200
_MAX_BODY_CHARS = 8 * 1024
_MAX_VERIFIER_OUTPUT_CHARS = 4 * 1024

SkipReason = Literal["no_repo_url", "no_commit_sha", "verifier_failed"]
AutoPRAction = Literal["created", "reused", "skipped"]


class AutoPROutput(BaseModel):
    """Caller-visible result of a :data:`AUTO_PR_GRAPH_ID` run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: AutoPRAction
    provider: str = Field(..., min_length=1)
    pr_ref: str | None
    pr_id: str | None
    title: str = Field(..., min_length=1, max_length=_MAX_TITLE_CHARS)
    body: str = Field(..., min_length=1, max_length=_MAX_BODY_CHARS)
    head_branch: str = Field(..., min_length=1)
    base_ref: str = Field(..., min_length=1)
    head_commit_sha: str | None
    reason: SkipReason | None = None


def _required_str(state: TaskRunState, key: str) -> str:
    raw = state.data.get(key)
    if not isinstance(raw, str) or not raw:
        raise GraphError(f"auto_pr: state.data[{key!r}] must be a non-empty str")
    return raw


def _optional_str(state: TaskRunState, key: str) -> str | None:
    raw = state.data.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise GraphError(f"auto_pr: state.data[{key!r}] must be a str or null")
    return raw or None


def _required_bool(state: TaskRunState, key: str) -> bool:
    raw = state.data.get(key)
    if not isinstance(raw, bool):
        raise GraphError(f"auto_pr: state.data[{key!r}] must be a bool")
    return raw


def _str_or_empty(state: TaskRunState, key: str) -> str:
    raw = state.data.get(key)
    return raw if isinstance(raw, str) else ""


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    suffix = "\n…[truncated]"
    return value[: max_chars - len(suffix)] + suffix


def _provider_label(git_provider: object | None) -> str:
    """Best-effort stable provider label for output-only attribution.

    Publish success paths use the adapter-returned ``PullRequestRef``
    and therefore do not depend on this helper. It only exists so the
    skipped path can still surface the configured provider rather than
    hard-coding ``fake``.
    """

    if git_provider is None:
        return "unknown"
    provider = getattr(git_provider, "_provider", None)
    if isinstance(provider, str) and provider:
        return provider
    provider = getattr(git_provider, "PROVIDER_NAME", None)
    if isinstance(provider, str) and provider:
        return provider
    return "unknown"


def _render_title(issue_title: str, override: str | None) -> str:
    raw = override if override else f"Fix: {issue_title}"
    return _truncate(raw, _MAX_TITLE_CHARS)


def _render_body(
    *,
    issue_title: str,
    issue_description: str | None,
    head_branch: str,
    base_ref: str,
    head_commit_sha: str,
    diff_stat: str,
    verifier_passed: bool,
    verifier_output: str,
) -> str:
    description_block = (
        issue_description.strip() if issue_description else "_no description provided_"
    )
    verifier_label = "passed" if verifier_passed else "failed"
    verifier_block = (
        _truncate(verifier_output.strip(), _MAX_VERIFIER_OUTPUT_CHARS) or "_no verifier output_"
    )
    diff_block = diff_stat.strip() or "_no diff stat_"
    rendered = (
        f"## {issue_title}\n\n"
        f"{description_block}\n\n"
        "### Change\n\n"
        f"- **Head**: `{head_branch}` @ `{head_commit_sha}`\n"
        f"- **Base**: `{base_ref}`\n\n"
        f"### Diff stat\n\n```\n{diff_block}\n```\n\n"
        f"### Verifier ({verifier_label})\n\n```\n{verifier_block}\n```\n"
    )
    return _truncate(rendered, _MAX_BODY_CHARS)


def build_auto_pr_graph(deps: GraphDeps) -> Graph:
    """Build the compiled :data:`AUTO_PR_GRAPH_ID` graph.

    The factory captures ``deps.git_provider`` once at materialization;
    it MUST be non-``None``. Callers that wire ``GraphDeps`` without a
    git provider get a clear graph-build-time check via the runtime
    ``GraphError`` raised inside ``prepare`` (so the registry can still
    introspect the graph even on workers that do not publish PRs).
    """

    git_provider = deps.git_provider
    provider_label = _provider_label(git_provider)

    async def prepare(state: TaskRunState) -> NodeResult:
        if git_provider is None:
            raise GraphError("auto_pr: GraphDeps.git_provider is required")

        base_ref = _required_str(state, "base_ref")
        head_branch = _required_str(state, "head_branch")
        issue_title = _required_str(state, "issue_title")
        verifier_passed = _required_bool(state, "verifier_passed")

        repo_url = _optional_str(state, "repo_url")
        head_commit_sha = _optional_str(state, "head_commit_sha")
        issue_description = _optional_str(state, "issue_description")
        pr_title_override = _optional_str(state, "pr_title_override")
        diff_stat = _str_or_empty(state, "diff_stat")
        verifier_output = _str_or_empty(state, "verifier_output")

        if repo_url is None:
            skip_reason: SkipReason | None = "no_repo_url"
        elif head_commit_sha is None:
            skip_reason = "no_commit_sha"
        elif not verifier_passed:
            skip_reason = "verifier_failed"
        else:
            skip_reason = None

        title = _render_title(issue_title, pr_title_override)
        body = _render_body(
            issue_title=issue_title,
            issue_description=issue_description,
            head_branch=head_branch,
            base_ref=base_ref,
            head_commit_sha=head_commit_sha or "<unknown>",
            diff_stat=diff_stat,
            verifier_passed=verifier_passed,
            verifier_output=verifier_output,
        )

        return NodeResult(
            data_update={
                "_auto_pr": {
                    "repo_url": repo_url,
                    "base_ref": base_ref,
                    "head_branch": head_branch,
                    "head_commit_sha": head_commit_sha,
                    "title": title,
                    "body": body,
                    "skip_reason": skip_reason,
                }
            }
        )

    async def publish(state: TaskRunState) -> NodeResult:
        if git_provider is None:
            raise GraphError("auto_pr: GraphDeps.git_provider is required")
        scratch_raw = state.data.get("_auto_pr")
        if not isinstance(scratch_raw, dict):
            raise GraphError("auto_pr: publish reached without _auto_pr scratch")
        scratch = {str(k): v for k, v in scratch_raw.items()}

        skip_reason = scratch.get("skip_reason")
        if skip_reason is not None:
            return NodeResult(
                data_update={"_auto_pr_publish": {"ref": None, "skip_reason": skip_reason}}
            )

        try:
            ref = await git_provider.open_or_reuse_pr(
                tenant_id=state.tenant_id,
                trace_id=state.trace_id,
                repo_url=str(scratch["repo_url"]),
                base_ref=str(scratch["base_ref"]),
                head_branch=str(scratch["head_branch"]),
                head_commit_sha=str(scratch["head_commit_sha"]),
                title=str(scratch["title"]),
                body=str(scratch["body"]),
            )
        except GitProviderError as exc:
            raise GraphError(f"auto_pr: git provider failed: {exc}") from exc

        return NodeResult(
            data_update={
                "_auto_pr_publish": {
                    "ref": ref.model_dump(mode="json"),
                    "skip_reason": None,
                }
            }
        )

    async def finalize(state: TaskRunState) -> NodeResult:
        scratch_raw = state.data.get("_auto_pr")
        publish_raw = state.data.get("_auto_pr_publish")
        if not isinstance(scratch_raw, dict) or not isinstance(publish_raw, dict):
            raise GraphError("auto_pr: finalize reached with malformed scratch")
        scratch = {str(k): v for k, v in scratch_raw.items()}
        published = {str(k): v for k, v in publish_raw.items()}

        head_branch = str(scratch["head_branch"])
        base_ref = str(scratch["base_ref"])
        title = str(scratch["title"])
        body = str(scratch["body"])
        head_commit_sha_raw = scratch.get("head_commit_sha")
        head_commit_sha = head_commit_sha_raw if isinstance(head_commit_sha_raw, str) else None

        ref_raw = published.get("ref")
        skip_reason_raw = published.get("skip_reason")
        action: AutoPRAction
        provider: str
        pr_ref: str | None
        pr_id: str | None
        reason: SkipReason | None
        if isinstance(ref_raw, dict):
            try:
                ref = PullRequestRef.model_validate(ref_raw)
            except ValidationError as exc:
                raise GraphError(f"auto_pr: invalid provider ref: {exc}") from exc
            action = ref.action
            provider = ref.provider
            pr_ref = ref.url
            pr_id = ref.pr_id
            reason = None
        else:
            if not isinstance(skip_reason_raw, str):
                raise GraphError("auto_pr: missing skip_reason on skipped path")
            action = "skipped"
            provider = provider_label
            pr_ref = None
            pr_id = None
            reason = skip_reason_raw  # type: ignore[assignment]

        try:
            output = AutoPROutput(
                action=action,
                provider=provider,
                pr_ref=pr_ref,
                pr_id=pr_id,
                title=title,
                body=body,
                head_branch=head_branch,
                base_ref=base_ref,
                head_commit_sha=head_commit_sha,
                reason=reason,
            )
        except ValidationError as exc:
            raise GraphError(f"auto_pr: invalid output shape: {exc}") from exc

        return NodeResult(data_update={"output": output.model_dump(mode="json")})

    g = Graph(AUTO_PR_GRAPH_ID)
    g.add_node("prepare", prepare)
    g.add_node("publish", publish)
    g.add_node("finalize", finalize)
    g.set_entry("prepare")
    g.add_edge("prepare", "publish")
    g.add_edge("publish", "finalize")
    g.add_edge("finalize", END)
    g.compile()
    return g
