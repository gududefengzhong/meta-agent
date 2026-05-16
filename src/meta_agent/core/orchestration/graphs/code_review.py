"""Built-in CODE_REVIEW graph: LLM-driven structured review of a diff.

Three nodes — ``prepare`` → ``review`` → ``finalize`` — that turn a
caller-supplied unified diff (plus optional context) into a strictly
typed :class:`ReviewOutput`. The graph never touches a worktree, never
shells out, never writes to disk: callers obtain the diff externally
(``git diff``, GitHub API, etc.) and feed it in via ``input_payload``.

Scope (v1):

* Input is a raw diff text. A workspace-mode that derives the diff from
  two refs is a strict superset and lives in a later milestone.
* Output is :class:`ReviewOutput` validated by pydantic v2; any field
  missing, mistyped, or out of range raises :class:`GraphError`.
* The LLM emits a single JSON object. Triple-backtick fencing is
  stripped before parsing (same trick ``bug_fix`` uses).
* "Scheme X" applies: a ``verdict`` of ``request_changes`` (or
  ``blocker``-severity findings) does NOT mark the task ``failed``.
  Only contract failures (malformed JSON / bad schema / oversize input)
  raise :class:`GraphError`. The caller decides whether to gate a merge.

Hard ceilings bound cost-runaway risk during the initial rollout:

* ``_MAX_DIFF_BYTES`` — refuse inputs that would balloon the prompt.
* ``_MAX_FINDINGS``  — refuse responses that try to flood the output.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from meta_agent.core.orchestration.deps import GraphDeps
from meta_agent.core.orchestration.graph import Graph, GraphError, NodeResult
from meta_agent.core.orchestration.state import END, TaskRunState
from meta_agent.core.ports.llm import (
    ChatMessage,
    LLMClient,
    LLMRequest,
    LLMResponse,
    MessageRole,
)

CODE_REVIEW_GRAPH_ID = "builtin.code_review"

_MAX_DIFF_BYTES = 64 * 1024
_MAX_FINDINGS = 50

_FENCE_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n```\s*$", re.DOTALL)

ReviewVerdict = Literal["approve", "request_changes", "comment"]
FindingCategory = Literal["bug", "regression", "security", "test_gap", "style", "other"]
FindingSeverity = Literal["blocker", "major", "minor", "info"]


class ReviewFinding(BaseModel):
    """A single reviewer observation about the diff."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: FindingCategory
    severity: FindingSeverity
    file: str | None = None
    line_range: str | None = None
    message: str = Field(..., min_length=1)
    suggested_action: str | None = None


class ReviewOutput(BaseModel):
    """Structured reviewer verdict produced by the LLM."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: ReviewVerdict
    summary: str = Field(..., min_length=1)
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=_MAX_FINDINGS)
    confidence: float = Field(..., ge=0.0, le=1.0)


def _required_str(state: TaskRunState, key: str) -> str:
    raw = state.data.get(key)
    if not isinstance(raw, str) or not raw:
        raise GraphError(f"code_review: state.data[{key!r}] must be a non-empty str")
    return raw


def _optional_str(state: TaskRunState, key: str) -> str | None:
    raw = state.data.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise GraphError(f"code_review: state.data[{key!r}] must be str when provided")
    return raw


def _optional_float(state: TaskRunState, key: str) -> float | None:
    raw = state.data.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise GraphError(f"code_review: state.data[{key!r}] must be a number when provided")
    return float(raw)


def _optional_int(state: TaskRunState, key: str) -> int | None:
    raw = state.data.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise GraphError(f"code_review: state.data[{key!r}] must be int when provided")
    return raw


def _strip_fence(text: str) -> str:
    match = _FENCE_RE.match(text.strip())
    return match.group(1) if match else text


def _build_request(state: TaskRunState) -> LLMRequest:
    diff_text = _required_str(state, "diff_text")
    if len(diff_text.encode("utf-8")) > _MAX_DIFF_BYTES:
        raise GraphError(f"code_review: diff_text exceeds max_diff_bytes={_MAX_DIFF_BYTES}")
    context = _optional_str(state, "context")
    pr_title = _optional_str(state, "pr_title")
    messages = _review_messages(diff_text=diff_text, context=context, pr_title=pr_title)
    return LLMRequest(
        messages=messages,
        model=_optional_str(state, "model"),
        temperature=_optional_float(state, "temperature"),
        max_tokens=_optional_int(state, "max_tokens"),
    )


def _review_messages(
    *, diff_text: str, context: str | None, pr_title: str | None
) -> tuple[ChatMessage, ...]:
    system = (
        "You are a senior code reviewer. Read the unified diff and any "
        "supplied context, then return ONLY a single JSON object matching "
        "this schema (no prose, no fences):\n"
        '{"verdict":"approve"|"request_changes"|"comment",'
        '"summary":"<one-paragraph reviewer summary>",'
        '"findings":[{"category":"bug"|"regression"|"security"|"test_gap"|"style"|"other",'
        '"severity":"blocker"|"major"|"minor"|"info",'
        '"file":"<repo-relative path or null>",'
        '"line_range":"<e.g. 12-18 or null>",'
        '"message":"<what is wrong and why>",'
        '"suggested_action":"<what to change or null>"}],'
        '"confidence":<float 0.0-1.0>}\n'
        f"Emit at most {_MAX_FINDINGS} findings. Prefer fewer, higher-signal "
        "findings over volume. Focus on behavior regressions, missing "
        "tests, security risks and obvious bugs."
    )
    user_parts: list[str] = []
    if pr_title:
        user_parts.append(f"PR title:\n{pr_title}")
    if context:
        user_parts.append(f"\nContext:\n{context}")
    user_parts.append(f"\nDiff:\n{diff_text}")
    return (
        ChatMessage(role=MessageRole.SYSTEM, content=system),
        ChatMessage(role=MessageRole.USER, content="\n".join(user_parts)),
    )


def _parse_review(raw: str) -> ReviewOutput:
    cleaned = _strip_fence(raw)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise GraphError(f"code_review: review response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise GraphError("code_review: review response must be a JSON object")
    try:
        return ReviewOutput.model_validate(payload)
    except ValidationError as exc:
        raise GraphError(f"code_review: review response failed schema: {exc.errors()}") from exc


def _project_output(review: ReviewOutput, response: LLMResponse) -> dict[str, object]:
    """Build the public ``state.data['output']`` payload.

    Mirrors ``simple_chat``: the review itself is the headline, but we
    also surface the model / finish_reason / usage so callers can audit
    cost and provider behaviour without joining ``llm_usage_logs``.
    """

    return {
        "verdict": review.verdict,
        "summary": review.summary,
        "findings": [f.model_dump(mode="json") for f in review.findings],
        "confidence": review.confidence,
        "model_used": response.model,
        "finish_reason": response.finish_reason,
        "usage": response.usage.model_dump(mode="json"),
        "provider_response_id": response.provider_response_id,
    }


def build_code_review_graph(deps: GraphDeps) -> Graph:
    """Return a fresh, compiled CODE_REVIEW graph bound to ``deps.llm``."""

    llm: LLMClient = deps.llm

    async def prepare(state: TaskRunState) -> NodeResult:
        request = _build_request(state)
        return NodeResult(data_update={"_llm_request": request.model_dump(mode="json")})

    async def review(state: TaskRunState) -> NodeResult:
        raw = state.data.get("_llm_request")
        if not isinstance(raw, dict):
            raise GraphError("code_review: prepare node did not persist _llm_request")
        request = LLMRequest.model_validate(raw)
        response = await llm.complete(request)
        parsed = _parse_review(response.content)
        return NodeResult(
            data_update={
                "_llm_response": response.model_dump(mode="json"),
                "_review": parsed.model_dump(mode="json"),
            }
        )

    async def finalize(state: TaskRunState) -> NodeResult:
        raw_resp = state.data.get("_llm_response")
        raw_review = state.data.get("_review")
        if not isinstance(raw_resp, dict) or not isinstance(raw_review, dict):
            raise GraphError("code_review: review node did not persist _llm_response/_review")
        response = LLMResponse.model_validate(raw_resp)
        review_obj = ReviewOutput.model_validate(raw_review)
        return NodeResult(data_update={"output": _project_output(review_obj, response)})

    g = Graph(CODE_REVIEW_GRAPH_ID)
    g.add_node("prepare", prepare)
    g.add_node("review", review)
    g.add_node("finalize", finalize)
    g.set_entry("prepare")
    g.add_edge("prepare", "review")
    g.add_edge("review", "finalize")
    g.add_edge("finalize", END)
    g.compile()
    return g
