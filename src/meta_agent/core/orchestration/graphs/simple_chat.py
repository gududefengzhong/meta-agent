"""Built-in single-turn chat graph powered by :class:`LLMClient`.

The graph wraps the LLM port in three nodes so that the same audit /
checkpoint plumbing used by every other task is exercised:

* ``prepare`` reads the request from ``state.data`` and assembles a
  validated :class:`LLMRequest`.
* ``call_llm`` invokes :meth:`LLMClient.complete` and stores the
  response on the state.
* ``finalize`` projects the response into a small, JSON-stable summary
  the worker / API layer can hand back to callers.

Errors are not swallowed: ``LLMTransientError`` and ``LLMRateLimitedError``
propagate so the worker's PEL-based retry kicks in, and non-retryable
``LLMError`` subclasses propagate too — the worker will eventually
mark the task ``FAILED`` after exhausting attempts. Mapping these to a
fail-fast ``state.error`` path is deferred to the result-contract
milestone (P1-F).
"""

from __future__ import annotations

from meta_agent.core.orchestration.deps import GraphDeps
from meta_agent.core.orchestration.graph import Graph, GraphError, NodeResult
from meta_agent.core.orchestration.llm_streaming import aggregate_stream_to_response
from meta_agent.core.orchestration.state import END, TaskRunState
from meta_agent.core.orchestration.step_kinds import STEP_CHAT
from meta_agent.core.ports.llm import (
    ChatMessage,
    LLMRequest,
    LLMResponse,
    MessageRole,
)

SIMPLE_CHAT_GRAPH_ID = "builtin.simple_chat"


def _str_or_none(state: TaskRunState, key: str) -> str | None:
    raw = state.data.get(key)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise GraphError(f"simple_chat: state.data[{key!r}] must be str, got {type(raw).__name__}")
    return raw


def _float_or_none(state: TaskRunState, key: str) -> float | None:
    raw = state.data.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise GraphError(
            f"simple_chat: state.data[{key!r}] must be a number, got {type(raw).__name__}"
        )
    return float(raw)


def _int_or_none(state: TaskRunState, key: str) -> int | None:
    raw = state.data.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise GraphError(f"simple_chat: state.data[{key!r}] must be int, got {type(raw).__name__}")
    return raw


def _build_request(state: TaskRunState) -> LLMRequest:
    user_prompt = _str_or_none(state, "user_prompt")
    if not user_prompt:
        raise GraphError("simple_chat: state.data['user_prompt'] is required")
    system_prompt = _str_or_none(state, "system_prompt")
    messages: list[ChatMessage] = []
    if system_prompt:
        messages.append(ChatMessage(role=MessageRole.SYSTEM, content=system_prompt))
    # δ-1 multi-turn: prior conversation from the same session,
    # loaded by the worker into ``_prior_messages``. See
    # :mod:`meta_agent.core.orchestration.session_history`.
    messages.extend(_load_prior_messages(state))
    messages.append(ChatMessage(role=MessageRole.USER, content=user_prompt))
    return LLMRequest(
        messages=tuple(messages),
        model=_str_or_none(state, "model"),
        temperature=_float_or_none(state, "temperature"),
        max_tokens=_int_or_none(state, "max_tokens"),
        step_kind=STEP_CHAT,
    )


def _load_prior_messages(state: TaskRunState) -> list[ChatMessage]:
    raw = state.data.get("_prior_messages")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise GraphError("simple_chat: state.data['_prior_messages'] must be a list")
    prior: list[ChatMessage] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise GraphError("simple_chat: _prior_messages entries must be objects")
        role_raw = entry.get("role")
        content_raw = entry.get("content")
        if not isinstance(role_raw, str) or not isinstance(content_raw, str):
            raise GraphError("simple_chat: _prior_messages entries need str role + content")
        try:
            role = MessageRole(role_raw)
        except ValueError as exc:
            raise GraphError(
                f"simple_chat: _prior_messages role {role_raw!r} is not a MessageRole"
            ) from exc
        prior.append(ChatMessage(role=role, content=content_raw))
    return prior


def _response_summary(response: LLMResponse) -> dict[str, object]:
    """Build the public dict written under ``state.data["output"]``.

    This is the only projection that ends up in :class:`TaskResult`;
    internal scratch keys (``_llm_request`` / ``_llm_response``) stay
    in ``state.data`` for checkpoint resume but never leak outward.
    """

    return {
        "assistant_message": response.content,
        "model_used": response.model,
        "finish_reason": response.finish_reason,
        "usage": response.usage.model_dump(mode="json"),
        "provider_response_id": response.provider_response_id,
    }


def build_simple_chat_graph(deps: GraphDeps) -> Graph:
    """Return a fresh, compiled simple-chat graph bound to ``deps.llm``."""

    llm = deps.llm

    async def prepare(state: TaskRunState) -> NodeResult:
        request = _build_request(state)
        return NodeResult(data_update={"_llm_request": request.model_dump(mode="json")})

    async def call_llm(state: TaskRunState) -> NodeResult:
        raw = state.data.get("_llm_request")
        if not isinstance(raw, dict):
            raise GraphError("simple_chat: prepare node did not persist _llm_request")
        request = LLMRequest.model_validate(raw)
        response = await aggregate_stream_to_response(llm, request)
        return NodeResult(data_update={"_llm_response": response.model_dump(mode="json")})

    async def finalize(state: TaskRunState) -> NodeResult:
        raw = state.data.get("_llm_response")
        if not isinstance(raw, dict):
            raise GraphError("simple_chat: call_llm node did not persist _llm_response")
        response = LLMResponse.model_validate(raw)
        return NodeResult(data_update={"output": _response_summary(response)})

    g = Graph(SIMPLE_CHAT_GRAPH_ID)
    g.add_node("prepare", prepare)
    g.add_node("call_llm", call_llm)
    g.add_node("finalize", finalize)
    g.set_entry("prepare")
    g.add_edge("prepare", "call_llm")
    g.add_edge("call_llm", "finalize")
    g.add_edge("finalize", END)
    g.compile()
    return g
