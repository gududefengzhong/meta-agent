"""Unit tests for the ``builtin.simple_chat`` graph."""

from __future__ import annotations

import pytest

from meta_agent.core.orchestration import GraphError, TaskRunState
from meta_agent.core.orchestration.graphs.simple_chat import (
    SIMPLE_CHAT_GRAPH_ID,
    build_simple_chat_graph,
)
from meta_agent.core.ports.llm import (
    LLMAuthError,
    LLMInvalidRequestError,
    LLMRateLimitedError,
    LLMRequest,
    LLMTransientError,
    MessageRole,
)
from tests.core.orchestration._fakes import FakeLLMClient, fake_deps, make_response

pytestmark = pytest.mark.asyncio


def _state(**data: object) -> TaskRunState:
    return TaskRunState(
        task_id="task-1",
        tenant_id="tenant-1",
        trace_id="trace-1",
        graph_id=SIMPLE_CHAT_GRAPH_ID,
        data=data,
    )


async def test_happy_path_sends_user_prompt_and_records_response() -> None:
    client = FakeLLMClient(response=make_response(content="hello back", model="fake/m1"))
    graph = build_simple_chat_graph(fake_deps(client))
    final = await graph.run(_state(user_prompt="hello"))

    assert final.finished is True
    output = final.data["output"]
    assert isinstance(output, dict)
    assert output["assistant_message"] == "hello back"
    assert output["model_used"] == "fake/m1"
    assert output["finish_reason"] == "stop"
    assert len(client.calls) == 1
    req = client.calls[0]
    assert [m.role for m in req.messages] == [MessageRole.USER]
    assert req.messages[0].content == "hello"


async def test_includes_system_prompt_when_provided() -> None:
    client = FakeLLMClient()
    graph = build_simple_chat_graph(fake_deps(client))
    await graph.run(_state(user_prompt="hi", system_prompt="be terse"))

    req = client.calls[0]
    assert [m.role for m in req.messages] == [MessageRole.SYSTEM, MessageRole.USER]
    assert req.messages[0].content == "be terse"
    assert req.messages[1].content == "hi"


async def test_propagates_model_override_from_state_data() -> None:
    client = FakeLLMClient()
    graph = build_simple_chat_graph(fake_deps(client))
    await graph.run(
        _state(user_prompt="hi", model="deepseek/deepseek-chat", temperature=0.2, max_tokens=64)
    )

    req: LLMRequest = client.calls[0]
    assert req.model == "deepseek/deepseek-chat"
    assert req.temperature == 0.2
    assert req.max_tokens == 64


async def test_missing_user_prompt_raises_graph_error() -> None:
    graph = build_simple_chat_graph(fake_deps())
    with pytest.raises(GraphError):
        await graph.run(_state())


async def test_non_string_user_prompt_raises_graph_error() -> None:
    graph = build_simple_chat_graph(fake_deps())
    with pytest.raises(GraphError):
        await graph.run(_state(user_prompt=123))


async def test_transient_error_propagates_for_pel_retry() -> None:
    def raise_transient(_req: LLMRequest) -> object:
        raise LLMTransientError("upstream 5xx")

    client = FakeLLMClient(handler=raise_transient)  # type: ignore[arg-type]
    graph = build_simple_chat_graph(fake_deps(client))
    with pytest.raises(LLMTransientError):
        await graph.run(_state(user_prompt="hi"))


async def test_rate_limit_error_propagates() -> None:
    def raise_rl(_req: LLMRequest) -> object:
        raise LLMRateLimitedError("429", retry_after=1.5)

    client = FakeLLMClient(handler=raise_rl)  # type: ignore[arg-type]
    graph = build_simple_chat_graph(fake_deps(client))
    with pytest.raises(LLMRateLimitedError):
        await graph.run(_state(user_prompt="hi"))


async def test_auth_error_propagates_uncategorised_as_logic() -> None:
    def raise_auth(_req: LLMRequest) -> object:
        raise LLMAuthError("401")

    client = FakeLLMClient(handler=raise_auth)  # type: ignore[arg-type]
    graph = build_simple_chat_graph(fake_deps(client))
    with pytest.raises(LLMAuthError):
        await graph.run(_state(user_prompt="hi"))


async def test_invalid_request_error_propagates() -> None:
    def raise_invalid(_req: LLMRequest) -> object:
        raise LLMInvalidRequestError("400")

    client = FakeLLMClient(handler=raise_invalid)  # type: ignore[arg-type]
    graph = build_simple_chat_graph(fake_deps(client))
    with pytest.raises(LLMInvalidRequestError):
        await graph.run(_state(user_prompt="hi"))


async def test_graph_id_matches_constant() -> None:
    graph = build_simple_chat_graph(fake_deps())
    assert graph.graph_id == SIMPLE_CHAT_GRAPH_ID


async def test_response_summary_includes_usage_dict() -> None:
    response = make_response(content="x", model="fake/m1")
    client = FakeLLMClient(response=response)
    graph = build_simple_chat_graph(fake_deps(client))
    final = await graph.run(_state(user_prompt="hi"))

    output = final.data["output"]
    assert isinstance(output, dict)
    usage = output["usage"]
    assert isinstance(usage, dict)
    assert usage["total_tokens"] == 2


async def test_three_checkpoints_one_per_node() -> None:
    """Each ``graph.step()`` advances the sequence; the run produces three."""
    client = FakeLLMClient()
    graph = build_simple_chat_graph(fake_deps(client))
    final = await graph.run(_state(user_prompt="hi"))
    assert final.sequence == 3
