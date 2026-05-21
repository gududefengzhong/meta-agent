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

from pathlib import Path
from typing import Any

from meta_agent.core.capabilities.executor import ToolExecutor
from meta_agent.core.capabilities.registry import ToolRegistry
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
from meta_agent.core.ports.tools import ToolCall, ToolContext, ToolResult, ToolSpec

SHELL_AGENT_GRAPH_ID = "builtin.shell_agent"

_DEFAULT_MAX_STEPS = 8
_DEFAULT_OUTPUT_BYTE_CAP = 65536


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


def _build_initial_messages(state: TaskRunState) -> list[ChatMessage]:
    user_prompt = _str_or_none(state, "user_prompt")
    if not user_prompt:
        raise GraphError("shell_agent: state.data['user_prompt'] is required")
    system_prompt = _str_or_none(state, "system_prompt")
    messages: list[ChatMessage] = []
    if system_prompt:
        messages.append(ChatMessage(role=MessageRole.SYSTEM, content=system_prompt))
    messages.append(ChatMessage(role=MessageRole.USER, content=user_prompt))
    return messages


def _load_messages(state: TaskRunState) -> list[ChatMessage]:
    raw = state.data.get("_messages")
    if raw is None:
        return _build_initial_messages(state)
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
) -> dict[str, object]:
    return {
        "assistant_message": response.content if response else "",
        "model_used": response.model if response else "",
        "finish_reason": response.finish_reason if response else "other",
        "steps": steps,
        "tool_invocations": tool_invocations,
        "truncated_by_max_steps": truncated_by_max_steps,
        "usage": response.usage.model_dump(mode="json") if response else {},
    }


def _require_tool_caps(deps: GraphDeps) -> tuple[ToolRegistry, ToolExecutor]:
    if deps.tool_registry is None or deps.tool_executor is None:
        raise GraphError(
            "shell_agent requires deps.tool_registry and deps.tool_executor; "
            "wire them through GraphDeps at boot"
        )
    return deps.tool_registry, deps.tool_executor


def build_shell_agent_graph(deps: GraphDeps) -> Graph:
    """Return a fresh, compiled shell_agent graph bound to ``deps``.

    Raises :class:`GraphError` if ``deps.tool_registry`` /
    ``deps.tool_executor`` are missing; both are mandatory because this
    graph's whole purpose is the tool-use loop.
    """

    llm: LLMClient = deps.llm
    registry, executor = _require_tool_caps(deps)

    async def plan(state: TaskRunState) -> NodeResult:
        messages = _load_messages(state)
        max_steps = _int_or_default(state, "max_steps", _DEFAULT_MAX_STEPS)
        if max_steps <= 0:
            raise GraphError("shell_agent: max_steps must be positive")
        step = _int_or_default(state, "_step", 0) + 1
        tool_names = _tool_names_or_none(state)
        specs = _select_specs(registry, tool_names)
        request = LLMRequest(
            messages=tuple(messages),
            model=_str_or_none(state, "model"),
            temperature=_float_or_none(state, "temperature"),
            max_tokens=_int_or_default(state, "max_tokens", 0) or None,
            tools=specs,
        )
        response = await llm.complete(request)
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
            [call.model_dump(mode="json") for call in response.tool_calls]
            if wants_tools
            else []
        )
        truncated = bool(response.tool_calls) and step >= max_steps
        return NodeResult(
            data_update={
                "_messages": _dump_messages(messages),
                "_step": step,
                "_pending_tool_calls": pending,
                "_plan_next": next_decision,
                "_last_response": response.model_dump(mode="json"),
                "_truncated_by_max_steps": (
                    bool(state.data.get("_truncated_by_max_steps")) or truncated
                ),
            }
        )

    async def tool_call(state: TaskRunState) -> NodeResult:
        raw = state.data.get("_pending_tool_calls")
        if not isinstance(raw, list) or not raw:
            raise GraphError("shell_agent: tool_call entered with no pending tool calls")
        ctx = _build_tool_context(state)
        messages = _load_messages(state)
        invocations = _int_or_default(state, "_tool_invocations", 0)
        for entry in raw:
            call = ToolCall.model_validate(entry)
            result: ToolResult = await executor.execute(call, ctx)
            invocations += 1
            messages.append(
                ChatMessage(
                    role=MessageRole.TOOL,
                    content=result.content,
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

    async def finalize(state: TaskRunState) -> NodeResult:
        raw = state.data.get("_last_response")
        response = LLMResponse.model_validate(raw) if isinstance(raw, dict) else None
        return NodeResult(
            data_update={
                "output": _output_summary(
                    response,
                    steps=_int_or_default(state, "_step", 0),
                    tool_invocations=_int_or_default(state, "_tool_invocations", 0),
                    truncated_by_max_steps=bool(state.data.get("_truncated_by_max_steps")),
                )
            }
        )

    def plan_router(state: TaskRunState) -> str:
        decision = state.data.get("_plan_next")
        if decision == "tool_call":
            return "tool_call"
        return "finalize"

    g = Graph(SHELL_AGENT_GRAPH_ID)
    g.add_node("plan", plan)
    g.add_node("tool_call", tool_call)
    g.add_node("finalize", finalize)
    g.set_entry("plan")
    g.add_conditional("plan", plan_router)
    g.add_edge("tool_call", "plan")
    g.add_edge("finalize", END)
    g.compile()
    return g
