"""Built-in ``shell_agent`` graph: minimal plan -> tool_call -> observe loop.

The graph wires the Phase β tool surface to the LLM port: each turn,
the LLM is shown the full conversation plus the tools advertised by
:attr:`GraphDeps.tool_registry`, and may either emit a final assistant
message or one or more :class:`ToolCall` instances. Tool calls are
dispatched through :attr:`GraphDeps.tool_executor` and the resulting
:class:`ToolResult` objects are appended as ``role=TOOL`` messages,
after which control returns to ``plan``.

Scope (v0):

* Single tool surface: whatever the injected registry exposes. The
  caller can narrow it per-run via ``state.data['tool_names']``
  (allow-list); ``None`` means "advertise everything in the registry".
* No streaming, no parallel-tool fan-out: tool calls are executed
  sequentially in the order the LLM emitted them. This matches how the
  executor's UTF-8 truncation guarantees observation bounds.
* ``max_steps`` caps the number of ``plan`` invocations (default 8).
  When the cap is hit while the LLM is still requesting tools, the
  loop short-circuits to ``finalize`` and marks
  ``output.truncated_by_max_steps`` so callers can detect it.
* Errors from individual tool calls become ``is_error=True`` tool
  messages (the executor normalises :class:`ToolError`); only unknown
  ``GraphError`` / unexpected exceptions surface as hard failures.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from meta_agent.core.capabilities.executor import ToolExecutor
from meta_agent.core.capabilities.registry import ToolRegistry
from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.domain.permission import PermissionDecision, PermissionPrompt
from meta_agent.core.orchestration.deps import GraphDeps
from meta_agent.core.orchestration.failure_explain import failure_explanation
from meta_agent.core.orchestration.graph import Graph, GraphError, NodeResult
from meta_agent.core.orchestration.llm_streaming import aggregate_stream_to_response
from meta_agent.core.orchestration.state import END, TaskRunState
from meta_agent.core.orchestration.step_kinds import STEP_PLAN
from meta_agent.core.ports.audit_sink import AuditSink
from meta_agent.core.ports.llm import (
    ChatMessage,
    LLMClient,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    MessageRole,
)
from meta_agent.core.ports.permission_gate import (
    PermissionGate,
    PermissionTimeoutError,
)
from meta_agent.core.ports.tools import ToolCall, ToolContext, ToolResult, ToolSpec

SHELL_AGENT_GRAPH_ID = "builtin.shell_agent"

logger = logging.getLogger(__name__)

_DEFAULT_MAX_STEPS = 8
_DEFAULT_OUTPUT_BYTE_CAP = 65536
_ERROR_EXCERPT_CHAR_LIMIT = 200
_PERMISSION_DECISION_TIMEOUT_S = 120.0
"""How long the agent waits for a user to decide on an inline permission prompt.

Two minutes is the rough upper bound where a connected user is
plausibly still attending; past that we assume they walked away.
The graph treats a timeout as a deny (same shape as an explicit
``allow=False``) so a silent client cannot accidentally green-light
a sensitive action.
"""
_AUDIT_ARGUMENT_PATH_KEYS = frozenset(
    {
        "path",
        "repo_path",
        "file",
        "file_path",
        "target",
        "targets",
        "path_globs",
        "suite",
    }
)


def _str_or_none(state: TaskRunState, key: str) -> str | None:
    raw = state.data.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise GraphError(f"shell_agent: state.data[{key!r}] must be str")
    return raw


def _int_or_default(state: TaskRunState, key: str, default: int) -> int:
    raw = state.data.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise GraphError(f"shell_agent: state.data[{key!r}] must be int")
    return raw


def _float_or_none(state: TaskRunState, key: str) -> float | None:
    raw = state.data.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise GraphError(f"shell_agent: state.data[{key!r}] must be a number")
    return float(raw)


def _positive_int_or_none(state: TaskRunState, key: str) -> int | None:
    raw = state.data.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise GraphError(f"shell_agent: state.data[{key!r}] must be a positive int")
    return raw


def _tool_names_or_none(state: TaskRunState) -> frozenset[str] | None:
    raw = state.data.get("tool_names")
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(name, str) for name in raw):
        raise GraphError("shell_agent: state.data['tool_names'] must be a list[str]")
    return frozenset(raw)


def _select_specs(registry: ToolRegistry, names: frozenset[str] | None) -> tuple[ToolSpec, ...]:
    specs = registry.list_specs()
    if names is None:
        return specs
    missing = names.difference({spec.name for spec in specs})
    if missing:
        raise GraphError(f"shell_agent: tool_names references unknown tools: {sorted(missing)}")
    return tuple(spec for spec in specs if spec.name in names)


def _build_initial_messages(
    state: TaskRunState, default_system_prompt: str | None = None
) -> list[ChatMessage]:
    user_prompt = _str_or_none(state, "user_prompt")
    if not user_prompt:
        raise GraphError("shell_agent: state.data['user_prompt'] is required")
    system_prompt = _str_or_none(state, "system_prompt") or default_system_prompt
    messages: list[ChatMessage] = []
    if system_prompt:
        messages.append(ChatMessage(role=MessageRole.SYSTEM, content=system_prompt))
    # δ-1 multi-turn: prepend the prior conversation thread from the
    # session (loaded by the worker into ``_prior_messages``) so the
    # model sees follow-up questions in context. The worker omits
    # this key entirely when there is no prior thread, so single-shot
    # tasks pay no cost.
    messages.extend(_load_prior_messages(state))
    messages.append(ChatMessage(role=MessageRole.USER, content=user_prompt))
    return messages


def _load_prior_messages(state: TaskRunState) -> list[ChatMessage]:
    raw = state.data.get("_prior_messages")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise GraphError("shell_agent: state.data['_prior_messages'] must be a list")
    prior: list[ChatMessage] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise GraphError("shell_agent: _prior_messages entries must be objects")
        role_raw = entry.get("role")
        content_raw = entry.get("content")
        if not isinstance(role_raw, str) or not isinstance(content_raw, str):
            raise GraphError("shell_agent: _prior_messages entries need str role + content")
        try:
            role = MessageRole(role_raw)
        except ValueError as exc:
            raise GraphError(
                f"shell_agent: _prior_messages role {role_raw!r} is not a MessageRole"
            ) from exc
        prior.append(ChatMessage(role=role, content=content_raw))
    return prior


def _load_messages(
    state: TaskRunState, default_system_prompt: str | None = None
) -> list[ChatMessage]:
    raw = state.data.get("_messages")
    if raw is None:
        return _build_initial_messages(state, default_system_prompt)
    if not isinstance(raw, list):
        raise GraphError("shell_agent: state.data['_messages'] must be a list")
    return [ChatMessage.model_validate(item) for item in raw]


def _dump_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    return [m.model_dump(mode="json") for m in messages]


def _build_tool_context(state: TaskRunState) -> ToolContext:
    ws_raw = state.data.get("_workspace_path")
    workspace = Path(ws_raw) if isinstance(ws_raw, str) and ws_raw else None
    cap = _int_or_default(state, "output_byte_cap", _DEFAULT_OUTPUT_BYTE_CAP)
    if cap <= 0:
        raise GraphError("shell_agent: output_byte_cap must be positive")
    return ToolContext(
        tenant_id=state.tenant_id,
        task_id=state.task_id,
        trace_id=state.trace_id,
        workspace_path=workspace,
        output_byte_cap=cap,
    )


def _output_summary(
    response: LLMResponse | None,
    *,
    steps: int,
    tool_invocations: int,
    truncated_by_max_steps: bool,
    truncated_by_token_budget: bool,
    usage: LLMUsage,
) -> dict[str, object]:
    failure = _shell_failure_explanation(
        truncated_by_max_steps=truncated_by_max_steps,
        truncated_by_token_budget=truncated_by_token_budget,
        steps=steps,
        tool_invocations=tool_invocations,
    )
    return {
        "assistant_message": response.content if response else "",
        "model_used": response.model if response else "",
        "finish_reason": response.finish_reason if response else "other",
        "steps": steps,
        "tool_invocations": tool_invocations,
        "truncated_by_max_steps": truncated_by_max_steps,
        "truncated_by_token_budget": truncated_by_token_budget,
        "usage": usage.model_dump(mode="json"),
        "failure_explanation": failure,
    }


def _shell_failure_explanation(
    *,
    truncated_by_max_steps: bool,
    truncated_by_token_budget: bool,
    steps: int,
    tool_invocations: int,
) -> dict[str, Any] | None:
    if truncated_by_token_budget:
        return failure_explanation(
            category="budget_exceeded",
            summary="Agent stopped before another LLM planning step because max_total_tokens was reached.",
            retryable=True,
            hints=[
                "Increase max_total_tokens for this task.",
                "Narrow the prompt or allowed files so fewer tokens are consumed.",
            ],
            details={"steps": steps, "tool_invocations": tool_invocations},
        )
    if truncated_by_max_steps:
        return failure_explanation(
            category="max_steps_truncated",
            summary="Agent stopped while the model was still requesting tools because max_steps was reached.",
            retryable=True,
            hints=[
                "Increase max_steps if the task is expected to require more tool turns.",
                "Inspect tool events for repeated or low-value calls before retrying.",
            ],
            details={"steps": steps, "tool_invocations": tool_invocations},
        )
    return None


def _merge_usage(prior: object, current: LLMUsage) -> dict[str, Any]:
    base = LLMUsage() if not isinstance(prior, dict) else LLMUsage.model_validate(prior)
    return LLMUsage(
        prompt_tokens=_sum_optional_int(base.prompt_tokens, current.prompt_tokens),
        completion_tokens=_sum_optional_int(base.completion_tokens, current.completion_tokens),
        total_tokens=_sum_optional_int(base.total_tokens, current.total_tokens),
        cost_usd_micros=_sum_optional_int(base.cost_usd_micros, current.cost_usd_micros),
    ).model_dump(mode="json")


def _usage_from_state(raw: object) -> LLMUsage:
    return LLMUsage.model_validate(raw) if isinstance(raw, dict) else LLMUsage()


def _sum_optional_int(left: int | None, right: int | None) -> int | None:
    if left is None:
        return right
    if right is None:
        return left
    return left + right


def _tool_message_content(result: ToolResult) -> str:
    lines: list[str] = []
    if result.is_error:
        lines.append("tool_status=error")
    if result.truncated:
        lines.append("tool_output_truncated=true")
    if result.metadata:
        lines.append(f"tool_metadata={json.dumps(result.metadata, sort_keys=True)}")
    lines.append(result.content)
    return "\n".join(lines)


def _error_excerpt(
    value: object,
    *,
    redact_text: Callable[[object], str] | None,
) -> str | None:
    text = "" if value is None else str(value)
    if not text:
        return None
    try:
        scrubbed = redact_text(text) if redact_text is not None else text
    except Exception:
        scrubbed = text
    if not scrubbed:
        return None
    return scrubbed[:_ERROR_EXCERPT_CHAR_LIMIT]


async def _execute_with_audit(
    call: ToolCall,
    tool_ctx: ToolContext,
    state: TaskRunState,
    operation: Awaitable[ToolResult],
    audit_sink: AuditSink | None,
    redact_text: Callable[[object], str] | None,
) -> ToolResult:
    """Run one LLM-requested tool call and emit structured audit events."""

    step = _int_or_default(state, "_step", 0)
    await _audit_tool_event(
        state,
        action="tool.invoked",
        audit_sink=audit_sink,
        payload={
            "call_id": call.id,
            "tool_name": call.name,
            "agent_step": step,
            "arguments": _summarize_tool_arguments(call.arguments),
            "workspace_path_present": tool_ctx.workspace_path is not None,
        },
    )
    started = time.perf_counter()
    try:
        result = await operation
    except Exception as exc:
        await _audit_tool_event(
            state,
            action="tool.failed",
            audit_sink=audit_sink,
            payload={
                "call_id": call.id,
                "tool_name": call.name,
                "agent_step": step,
                "duration_ms": _elapsed_ms(started),
                "error_type": type(exc).__name__,
                "error_excerpt": _error_excerpt(str(exc), redact_text=redact_text),
            },
        )
        raise
    payload: dict[str, object] = {
        "call_id": result.call_id,
        "tool_name": result.name,
        "agent_step": step,
        "duration_ms": _elapsed_ms(started),
        "output_bytes": len(result.content.encode("utf-8")),
        "truncated": result.truncated,
        "metadata": dict(result.metadata),
    }
    if result.is_error:
        payload["error_excerpt"] = _error_excerpt(result.content, redact_text=redact_text)
    action = "tool.failed" if result.is_error else "tool.completed"
    await _audit_tool_event(state, action=action, audit_sink=audit_sink, payload=payload)
    return result


def _elapsed_ms(started: float) -> int:
    delta = time.perf_counter() - started
    if delta < 0:
        return 0
    return int(delta * 1000)


async def _audit_tool_event(
    state: TaskRunState,
    *,
    action: str,
    audit_sink: AuditSink | None,
    payload: dict[str, object],
) -> None:
    if audit_sink is None:
        return
    try:
        await audit_sink.append(
            AuditEvent(
                event_id=f"aud-{uuid.uuid4()}",
                tenant_id=state.tenant_id,
                principal_id="system",
                session_id=None,
                task_id=state.task_id,
                trace_id=state.trace_id,
                action=action,
                payload=payload,
                occurred_at=datetime.now(UTC),
            )
        )
    except Exception as exc:
        logger.warning(
            "shell_agent.tool_audit_failed",
            extra={
                "task_id": state.task_id,
                "action": action,
                "error_type": type(exc).__name__,
            },
        )


def _summarize_tool_arguments(args: dict[str, Any]) -> dict[str, object]:
    """Return a non-secret-bearing argument summary for audit payloads."""

    return {key: _summarize_argument_value(key, value) for key, value in args.items()}


def _summarize_argument_value(key: str, value: object) -> object:
    lower = key.lower()
    if lower in _AUDIT_ARGUMENT_PATH_KEYS:
        return _summarize_path_like(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return {
            "type": "str",
            "chars": len(value),
            "sha256": sha256(value.encode("utf-8")).hexdigest()[:12],
        }
    if isinstance(value, list | tuple):
        return {"type": "list", "items": len(value)}
    if isinstance(value, dict):
        return {"type": "object", "keys": sorted(str(k) for k in value)[:20]}
    return {"type": type(value).__name__}


def _summarize_path_like(value: object) -> object:
    if isinstance(value, str):
        return value[:200]
    if isinstance(value, list | tuple):
        out: list[object] = []
        for item in value[:20]:
            out.append(item[:200] if isinstance(item, str) else _summarize_argument_value("", item))
        return out
    return _summarize_argument_value("", value)


async def _gated_execute(
    call: ToolCall,
    tool_ctx: ToolContext,
    executor: ToolExecutor,
    *,
    gate: PermissionGate,
    state: TaskRunState,
) -> ToolResult:
    """Ask the connected client before executing ``call``.

    Returns a synthetic :class:`ToolResult` carrying ``is_error=True``
    when the user denies the action or doesn't respond inside the
    timeout. The agent sees the denial in the conversation and can
    replan; we deliberately do not raise here so a single denied
    action doesn't abort the whole task.
    """

    prompt = PermissionPrompt(
        prompt_id=f"prm-{uuid.uuid4()}",
        tenant_id=state.tenant_id,
        task_id=state.task_id,
        tool_name=call.name,
        summary=f"Run tool {call.name!r}",
        payload=dict(call.arguments),
        created_at=datetime.now(UTC),
    )
    try:
        decision = await gate.request(prompt, timeout_seconds=_PERMISSION_DECISION_TIMEOUT_S)
    except PermissionTimeoutError:
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content="[permission_timeout] user did not respond in time; tool was skipped",
            is_error=True,
            metadata={
                "permission_outcome": "timeout",
                "prompt_id": prompt.prompt_id,
            },
        )
    if not decision.allow:
        reason_suffix = f": {decision.reason}" if decision.reason else ""
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content=f"[permission_denied] user denied this tool call{reason_suffix}",
            is_error=True,
            metadata={
                "permission_outcome": "denied",
                "prompt_id": prompt.prompt_id,
            },
        )
    return await executor.execute(call, tool_ctx)


_PLAN_PROMPT_TOOL_NAME = "<plan>"
"""Sentinel ``tool_name`` for plan-mode prompts.

Lets a single permission prompt carry the assistant's planning
step (text + a batch of proposed tool calls) without changing the
:class:`PermissionPrompt` schema. Clients render plan prompts
differently from per-tool prompts by branching on this value.
"""


async def _request_plan_decision(
    state: TaskRunState,
    calls: list[ToolCall],
    gate: PermissionGate,
) -> PermissionDecision | None:
    """Emit one gate covering all pending tool calls; return the decision.

    Returns ``None`` when the gate times out so the caller can treat
    every tool as denied (the safer default — silent client must
    not green-light a batch action). ``state.data`` is mutated with
    the assigned ``_plan_prompt_id`` so synthetic deny ToolResults
    can carry it as metadata for trace correlation.
    """

    prompt_id = f"prm-plan-{uuid.uuid4()}"
    state.data["_plan_prompt_id"] = prompt_id  # for trace metadata only
    summary = _str_or_none(state, "_pending_plan_summary") or ""
    prompt = PermissionPrompt(
        prompt_id=prompt_id,
        tenant_id=state.tenant_id,
        task_id=state.task_id,
        tool_name=_PLAN_PROMPT_TOOL_NAME,
        summary=summary,
        payload={
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}
                for call in calls
            ],
        },
        created_at=datetime.now(UTC),
    )
    try:
        return await gate.request(prompt, timeout_seconds=_PERMISSION_DECISION_TIMEOUT_S)
    except PermissionTimeoutError:
        return None


def _plan_prompt_id_from_state(state: TaskRunState) -> str:
    raw = state.data.get("_plan_prompt_id")
    return raw if isinstance(raw, str) else ""


def _synthetic_deny_result(
    call: ToolCall,
    *,
    reason: str | None,
    outcome: str,
    prompt_id: str,
) -> ToolResult:
    if outcome == "timeout":
        content = "[plan_timeout] user did not respond in time; tool was skipped"
    else:
        reason_suffix = f": {reason}" if reason else ""
        content = f"[plan_denied] user denied the plan{reason_suffix}"
    return ToolResult(
        call_id=call.id,
        name=call.name,
        content=content,
        is_error=True,
        metadata={
            "permission_outcome": outcome,
            "prompt_id": prompt_id,
        },
    )


async def _synthetic_deny_result_async(
    call: ToolCall,
    *,
    reason: str | None,
    outcome: str,
    prompt_id: str,
) -> ToolResult:
    return _synthetic_deny_result(
        call,
        reason=reason,
        outcome=outcome,
        prompt_id=prompt_id,
    )


def _require_tool_caps(deps: GraphDeps) -> tuple[ToolRegistry, ToolExecutor]:
    if deps.tool_registry is None or deps.tool_executor is None:
        raise GraphError(
            "shell_agent requires deps.tool_registry and deps.tool_executor; "
            "wire them through GraphDeps at boot"
        )
    return deps.tool_registry, deps.tool_executor


def build_shell_agent_graph(
    deps: GraphDeps,
    *,
    graph_id: str = SHELL_AGENT_GRAPH_ID,
    default_system_prompt: str | None = None,
    default_system_prompt_id: str | None = None,
) -> Graph:
    """Return a fresh, compiled shell_agent graph bound to ``deps``.

    ``graph_id`` defaults to :data:`SHELL_AGENT_GRAPH_ID`. Callers that
    want to reuse the same plan→tool→observe loop under a distinct
    audit / registry identity pass their own id; the graph topology
    and node behavior are identical.

    System-prompt resolution at plan time (first matching rule wins):

    1. ``state.data['system_prompt']`` (caller-supplied raw text). No
       ``prompt_id`` is attached to the outgoing LLMRequest in this
       case — the caller owns provenance.
    2. ``default_system_prompt_id`` resolved through
       ``deps.prompt_registry``. The resulting ``prompt_id`` +
       ``version`` are attached to every LLMRequest the graph makes,
       so ``llm_usage_logs`` can join back to the exact registered
       template.
    3. ``default_system_prompt`` (legacy raw text). No ``prompt_id``
       attribution.
    4. No system message.

    Raises :class:`GraphError` if ``deps.tool_registry`` /
    ``deps.tool_executor`` are missing; both are mandatory because this
    graph's whole purpose is the tool-use loop. Also raises if
    ``default_system_prompt_id`` is set but ``deps.prompt_registry`` is
    not.
    """

    llm: LLMClient = deps.llm
    registry, executor = _require_tool_caps(deps)
    # The prompt-registry guard fires at plan-time, not build-time, so
    # bootstraps that pre-register graphs without a wired registry can
    # still materialise the registry mapping. The first plan call that
    # actually needs the registry raises if it is still missing.

    async def _resolve_default_prompt(
        state: TaskRunState,
    ) -> tuple[str | None, str | None, int | None]:
        """Resolve the default system prompt + its registry identity.

        Returns ``(content, prompt_id, version)``:

        * When ``state.data['system_prompt']`` is set the caller already
          supplied the rendered text via ``_load_messages``; we return
          ``content=None`` so ``_load_messages`` keeps using the caller
          text, and we pass through any caller-supplied
          ``system_prompt_id`` / ``system_prompt_version`` as
          provenance. This is the path used by graphs that compose
          shell_agent (e.g. bug_fix_v2) and render templates themselves.
        * Otherwise, if the builder was wired with
          ``default_system_prompt_id``, we fetch from the registry and
          return the asset's content and full provenance.
        * Otherwise we fall back to the legacy raw
          ``default_system_prompt`` with no provenance.
        """

        if _str_or_none(state, "system_prompt"):
            sid = _str_or_none(state, "system_prompt_id")
            sver_raw = state.data.get("system_prompt_version")
            if sver_raw is not None and (
                isinstance(sver_raw, bool) or not isinstance(sver_raw, int)
            ):
                raise GraphError("shell_agent: state.data['system_prompt_version'] must be int")
            sver = sver_raw if isinstance(sver_raw, int) else None
            return None, sid, sver
        if default_system_prompt_id is None:
            return default_system_prompt, None, None
        if deps.prompt_registry is None:
            raise GraphError(
                "shell_agent: default_system_prompt_id requires deps.prompt_registry; "
                "wire a PromptRegistry through GraphDeps at boot"
            )
        asset = await deps.prompt_registry.fetch(
            default_system_prompt_id, tenant_id=state.tenant_id
        )
        return asset.content, asset.prompt_id, asset.version

    async def plan(state: TaskRunState) -> NodeResult:
        default_sp, default_sp_id, default_sp_ver = await _resolve_default_prompt(state)
        messages = _load_messages(state, default_sp)
        max_steps = _int_or_default(state, "max_steps", _DEFAULT_MAX_STEPS)
        if max_steps <= 0:
            raise GraphError("shell_agent: max_steps must be positive")
        max_total_tokens = _positive_int_or_none(state, "max_total_tokens")
        usage_so_far = _usage_from_state(state.data.get("_usage"))
        if (
            max_total_tokens is not None
            and usage_so_far.total_tokens is not None
            and usage_so_far.total_tokens >= max_total_tokens
        ):
            return NodeResult(
                data_update={
                    "_plan_next": "finalize",
                    "_truncated_by_token_budget": True,
                }
            )
        step = _int_or_default(state, "_step", 0) + 1
        tool_names = _tool_names_or_none(state)
        specs = _select_specs(registry, tool_names)
        request = LLMRequest(
            messages=tuple(messages),
            model=_str_or_none(state, "model"),
            temperature=_float_or_none(state, "temperature"),
            max_tokens=_int_or_default(state, "max_tokens", 0) or None,
            tools=specs,
            prompt_id=default_sp_id,
            prompt_version=default_sp_ver,
            step_kind=STEP_PLAN,
        )
        response = await aggregate_stream_to_response(llm, request)
        messages.append(
            ChatMessage(
                role=MessageRole.ASSISTANT,
                content=response.content,
                tool_calls=response.tool_calls,
            )
        )
        wants_tools = bool(response.tool_calls) and step < max_steps
        next_decision = "tool_call" if wants_tools else "finalize"
        pending = (
            [call.model_dump(mode="json") for call in response.tool_calls] if wants_tools else []
        )
        truncated = bool(response.tool_calls) and step >= max_steps
        merged_usage = _merge_usage(state.data.get("_usage"), response.usage)
        merged_usage_obj = LLMUsage.model_validate(merged_usage)
        budget_truncated = (
            max_total_tokens is not None
            and merged_usage_obj.total_tokens is not None
            and merged_usage_obj.total_tokens >= max_total_tokens
        )
        return NodeResult(
            data_update={
                "_messages": _dump_messages(messages),
                "_step": step,
                "_pending_tool_calls": pending,
                "_plan_next": next_decision,
                "_last_response": response.model_dump(mode="json"),
                # δ-1 plan mode: cache the plan summary (assistant
                # text) so ``tool_call`` can carry it on the gate
                # prompt without re-reading messages. Cheap to store
                # even when plan mode isn't active — overwritten each
                # planning step.
                "_pending_plan_summary": response.content,
                "_usage": merged_usage,
                "_truncated_by_max_steps": (
                    bool(state.data.get("_truncated_by_max_steps")) or truncated
                ),
                "_truncated_by_token_budget": (
                    bool(state.data.get("_truncated_by_token_budget")) or budget_truncated
                ),
            }
        )

    async def tool_call(state: TaskRunState) -> NodeResult:
        raw = state.data.get("_pending_tool_calls")
        if not isinstance(raw, list) or not raw:
            raise GraphError("shell_agent: tool_call entered with no pending tool calls")
        ctx = _build_tool_context(state)
        messages = _load_messages(state, default_system_prompt)
        invocations = _int_or_default(state, "_tool_invocations", 0)
        permission_mode = _str_or_none(state, "_permission_mode")
        gate = deps.permission_gate
        calls = [ToolCall.model_validate(entry) for entry in raw]

        # Plan mode: one gate covering the whole planning step. The
        # prompt carries the assistant's plan text + every pending
        # tool call so the user reviews the batch holistically.
        if permission_mode == "plan" and gate is not None:
            plan_decision = await _request_plan_decision(state, calls, gate)
            for call in calls:
                result: ToolResult
                if plan_decision is None or plan_decision.allow:
                    operation = executor.execute(call, ctx)
                else:
                    operation = _synthetic_deny_result_async(
                        call,
                        reason=plan_decision.reason if plan_decision else None,
                        outcome="timeout" if plan_decision is None else "denied",
                        prompt_id=_plan_prompt_id_from_state(state),
                    )
                result = await _execute_with_audit(
                    call,
                    ctx,
                    state,
                    operation,
                    deps.audit_sink,
                    deps.redact_text,
                )
                invocations += 1
                messages.append(
                    ChatMessage(
                        role=MessageRole.TOOL,
                        content=_tool_message_content(result),
                        tool_call_id=result.call_id,
                    )
                )
            return NodeResult(
                data_update={
                    "_messages": _dump_messages(messages),
                    "_pending_tool_calls": [],
                    "_tool_invocations": invocations,
                }
            )

        # Per-tool gate (approve_each_tool) or no gate (auto).
        requires_approval = permission_mode == "approve_each_tool" and gate is not None
        for call in calls:
            tool_result: ToolResult
            if requires_approval:
                assert gate is not None  # narrowed by the guard above
                operation = _gated_execute(
                    call,
                    ctx,
                    executor,
                    gate=gate,
                    state=state,
                )
                tool_result = await _execute_with_audit(
                    call,
                    ctx,
                    state,
                    operation,
                    deps.audit_sink,
                    deps.redact_text,
                )
            else:
                operation = executor.execute(call, ctx)
                tool_result = await _execute_with_audit(
                    call,
                    ctx,
                    state,
                    operation,
                    deps.audit_sink,
                    deps.redact_text,
                )
            invocations += 1
            messages.append(
                ChatMessage(
                    role=MessageRole.TOOL,
                    content=_tool_message_content(tool_result),
                    tool_call_id=tool_result.call_id,
                )
            )
        return NodeResult(
            data_update={
                "_messages": _dump_messages(messages),
                "_pending_tool_calls": [],
                "_tool_invocations": invocations,
            }
        )

    async def finalize(state: TaskRunState) -> NodeResult:
        raw = state.data.get("_last_response")
        response = LLMResponse.model_validate(raw) if isinstance(raw, dict) else None
        usage = _usage_from_state(state.data.get("_usage"))
        return NodeResult(
            data_update={
                "output": _output_summary(
                    response,
                    steps=_int_or_default(state, "_step", 0),
                    tool_invocations=_int_or_default(state, "_tool_invocations", 0),
                    truncated_by_max_steps=bool(state.data.get("_truncated_by_max_steps")),
                    truncated_by_token_budget=bool(state.data.get("_truncated_by_token_budget")),
                    usage=usage,
                )
            }
        )

    def plan_router(state: TaskRunState) -> str:
        decision = state.data.get("_plan_next")
        if decision == "tool_call":
            return "tool_call"
        return "finalize"

    g = Graph(graph_id)
    g.add_node("plan", plan)
    g.add_node("tool_call", tool_call)
    g.add_node("finalize", finalize)
    g.set_entry("plan")
    g.add_conditional("plan", plan_router)
    g.add_edge("tool_call", "plan")
    g.add_edge("finalize", END)
    g.compile()
    return g
