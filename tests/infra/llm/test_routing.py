"""Unit tests for :class:`StaticLLMRouter` and :class:`RoutingLLMClient`."""

from __future__ import annotations

import pytest

from meta_agent.core.ports.llm import (
    ChatMessage,
    LLMClient,
    LLMRequest,
    LLMResponse,
    LLMUsage,
    MessageRole,
)
from meta_agent.infra.llm.routing import RoutingLLMClient, StaticLLMRouter


class _RecordingLLM(LLMClient):
    """LLM stub that captures every request it sees."""

    def __init__(self) -> None:
        self.calls: list[LLMRequest] = []
        self.closed = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        return LLMResponse(
            content="ok",
            model=request.model or "fake/default",
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    async def close(self) -> None:
        self.closed = True


def _msg() -> tuple[ChatMessage, ...]:
    return (ChatMessage(role=MessageRole.USER, content="hi"),)


async def test_static_router_returns_mapped_model() -> None:
    router = StaticLLMRouter(
        {
            "plan": "deepseek/deepseek-chat",
            "edit": "qwen/qwen3-coder",
        }
    )
    assert await router.select_model(step_kind="plan") == "deepseek/deepseek-chat"
    assert await router.select_model(step_kind="edit") == "qwen/qwen3-coder"


async def test_static_router_returns_none_for_unknown_step_kind() -> None:
    router = StaticLLMRouter({"plan": "x/y"})
    assert await router.select_model(step_kind="never-defined") is None


async def test_static_router_rejects_empty_step_kind_key() -> None:
    with pytest.raises(ValueError, match="step_kind"):
        StaticLLMRouter({"": "x"})


async def test_static_router_rejects_blank_model_value() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        StaticLLMRouter({"plan": "   "})


async def test_routing_client_overrides_model_for_tagged_request() -> None:
    inner = _RecordingLLM()
    routed = RoutingLLMClient(inner, StaticLLMRouter({"plan": "deepseek/deepseek-chat"}))

    await routed.complete(LLMRequest(messages=_msg(), model="caller/initial", step_kind="plan"))

    assert len(inner.calls) == 1
    assert inner.calls[0].model == "deepseek/deepseek-chat"
    # step_kind survives the override so MeteredLLMClient still records it.
    assert inner.calls[0].step_kind == "plan"


async def test_routing_client_passes_request_through_when_no_step_kind() -> None:
    inner = _RecordingLLM()
    routed = RoutingLLMClient(inner, StaticLLMRouter({"plan": "x/y"}))

    await routed.complete(LLMRequest(messages=_msg(), model="caller/initial"))

    assert inner.calls[0].model == "caller/initial"


async def test_routing_client_passes_through_when_router_returns_none() -> None:
    inner = _RecordingLLM()
    routed = RoutingLLMClient(inner, StaticLLMRouter({"plan": "x/y"}))

    await routed.complete(LLMRequest(messages=_msg(), model="caller/initial", step_kind="observe"))

    assert inner.calls[0].model == "caller/initial"


async def test_routing_client_no_op_when_router_value_matches_existing_model() -> None:
    # The same request instance flows through — no model_copy allocation.
    inner = _RecordingLLM()
    routed = RoutingLLMClient(inner, StaticLLMRouter({"plan": "deepseek/deepseek-chat"}))
    request = LLMRequest(messages=_msg(), model="deepseek/deepseek-chat", step_kind="plan")
    await routed.complete(request)

    assert inner.calls[0] is request


async def test_close_chains_to_inner() -> None:
    inner = _RecordingLLM()
    routed = RoutingLLMClient(inner, StaticLLMRouter({}))
    await routed.close()
    assert inner.closed is True
