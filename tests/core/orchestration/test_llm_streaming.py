"""Unit tests for :mod:`meta_agent.core.orchestration.llm_streaming`.

Covers two contracts:

* :class:`StreamAggregator` reassembles chunks into a synthetic
  :class:`LLMResponse` that matches what a non-streaming call would
  have returned for the same upstream interaction (content
  concatenation, per-index tool-call merging, last-observed metadata).
* :func:`aggregate_stream_to_response` is functionally equivalent to
  ``llm.complete(request)`` when both paths receive the same logical
  upstream events. The graph migration relies on this — graphs that
  used to call ``complete`` should observe no behavioural diff.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from meta_agent.core.orchestration.llm_streaming import (
    StreamAggregator,
    aggregate_stream_to_response,
)
from meta_agent.core.ports.llm import (
    ChatMessage,
    LLMClient,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    LLMUsage,
    MessageRole,
    ToolCallDelta,
)
from meta_agent.core.ports.tools import ToolCall


def _request(model: str | None = "openai/gpt-4o") -> LLMRequest:
    return LLMRequest(
        messages=(ChatMessage(role=MessageRole.USER, content="hi"),),
        model=model,
    )


class _StreamingFake(LLMClient):
    """LLMClient whose ``stream`` yields canned chunks; ``complete`` is unused."""

    def __init__(self, chunks: list[LLMStreamChunk]) -> None:
        self._chunks = list(chunks)
        self.stream_calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise AssertionError("complete must not be called when graph uses streaming")

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        self.stream_calls += 1
        for chunk in self._chunks:
            yield chunk

    async def close(self) -> None:
        return None


async def test_aggregator_concatenates_content_deltas_in_order() -> None:
    agg = StreamAggregator()
    for piece in ("he", "ll", "o"):
        agg.observe(LLMStreamChunk(content_delta=piece))
    agg.observe(LLMStreamChunk(finish_reason="stop"))
    response = agg.to_response(_request())
    assert response.content == "hello"
    assert response.finish_reason == "stop"


async def test_aggregator_merges_tool_call_deltas_per_index() -> None:
    agg = StreamAggregator()
    agg.observe(
        LLMStreamChunk(
            tool_call_deltas=(
                ToolCallDelta(index=0, id="call_a", name="fs_read", arguments_delta='{"pa'),
                ToolCallDelta(index=1, id="call_b", name="shell", arguments_delta='{"c'),
            )
        )
    )
    agg.observe(
        LLMStreamChunk(
            tool_call_deltas=(
                ToolCallDelta(index=0, arguments_delta='th":"a.py"}'),
                ToolCallDelta(index=1, arguments_delta='md":"ls"}'),
            )
        )
    )
    agg.observe(LLMStreamChunk(finish_reason="tool_call"))
    response = agg.to_response(_request())
    assert len(response.tool_calls) == 2
    call_a, call_b = response.tool_calls
    assert call_a == ToolCall(id="call_a", name="fs_read", arguments={"path": "a.py"})
    assert call_b == ToolCall(id="call_b", name="shell", arguments={"cmd": "ls"})


async def test_aggregator_uses_last_observed_usage_model_and_response_id() -> None:
    agg = StreamAggregator()
    agg.observe(LLMStreamChunk(content_delta="x", model="early-model"))
    agg.observe(
        LLMStreamChunk(
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=3, completion_tokens=5, total_tokens=8),
            model="final-model",
            provider_response_id="resp-1",
        )
    )
    response = agg.to_response(_request())
    assert response.model == "final-model"
    assert response.usage.total_tokens == 8
    assert response.provider_response_id == "resp-1"


async def test_aggregator_defaults_finish_reason_when_provider_omits() -> None:
    agg = StreamAggregator()
    agg.observe(LLMStreamChunk(content_delta="content but no finish"))
    response = agg.to_response(_request())
    assert response.finish_reason == "other"


async def test_aggregator_drops_incomplete_tool_call_fragments() -> None:
    # A tool call with only ``arguments_delta`` and no ``id`` / ``name`` is
    # malformed — the synthetic response skips it rather than fabricating
    # placeholder identifiers.
    agg = StreamAggregator()
    agg.observe(LLMStreamChunk(tool_call_deltas=(ToolCallDelta(index=0, arguments_delta="{}"),)))
    agg.observe(LLMStreamChunk(finish_reason="tool_call"))
    response = agg.to_response(_request())
    assert response.tool_calls == ()


async def test_aggregator_handles_malformed_arguments_json() -> None:
    agg = StreamAggregator()
    agg.observe(
        LLMStreamChunk(
            tool_call_deltas=(
                ToolCallDelta(index=0, id="c", name="t", arguments_delta="not valid json"),
            )
        )
    )
    agg.observe(LLMStreamChunk(finish_reason="tool_call"))
    response = agg.to_response(_request())
    # Malformed args → empty dict, not a raise — graph nodes are
    # defensive about tool-call arguments anyway.
    assert response.tool_calls == (ToolCall(id="c", name="t", arguments={}),)


async def test_aggregate_stream_to_response_drives_inner_stream() -> None:
    fake = _StreamingFake(
        [
            LLMStreamChunk(content_delta="he"),
            LLMStreamChunk(content_delta="llo"),
            LLMStreamChunk(
                finish_reason="stop",
                usage=LLMUsage(prompt_tokens=3, completion_tokens=5, total_tokens=8),
                model="openai/gpt-4o",
                provider_response_id="r-1",
            ),
        ]
    )
    response = await aggregate_stream_to_response(fake, _request())
    assert fake.stream_calls == 1
    assert response.content == "hello"
    assert response.finish_reason == "stop"
    assert response.usage.total_tokens == 8
    assert response.model == "openai/gpt-4o"
    assert response.provider_response_id == "r-1"


async def test_aggregate_stream_to_response_propagates_inner_error() -> None:
    class _BrokenFake(LLMClient):
        async def complete(self, request: LLMRequest) -> LLMResponse:
            raise AssertionError("not used")

        async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
            yield LLMStreamChunk(content_delta="partial")
            raise RuntimeError("upstream blew up mid-stream")

        async def close(self) -> None:
            return None

    with pytest.raises(RuntimeError, match="upstream blew up"):
        await aggregate_stream_to_response(_BrokenFake(), _request())


async def test_helper_drives_broadcaster_when_wrapped_in_broadcasting_layer() -> None:
    """End-to-end: aggregate_stream_to_response + BroadcastingLLMClient + bound ctx
    publishes each chunk to the per-task channel.

    This is the integration that justifies the graph migration: chunks
    flow through the production decorator stack to the SSE wire while
    the graph node still receives a buffered :class:`LLMResponse`.
    """

    from meta_agent.core.ports.chunk_broadcaster import ChunkBroadcaster
    from meta_agent.infra.llm.broadcasting import BroadcastingLLMClient
    from meta_agent.infra.security.context import RequestContext, bind_context

    class _RecordingBroadcaster(ChunkBroadcaster):
        def __init__(self) -> None:
            self.published: list[tuple[str, str, LLMStreamChunk]] = []

        async def publish(self, *, tenant_id: str, task_id: str, chunk: LLMStreamChunk) -> None:
            self.published.append((tenant_id, task_id, chunk))

        async def subscribe(self, *, tenant_id: str, task_id: str):  # type: ignore[no-untyped-def]
            raise AssertionError("subscribe not exercised by this test")

        async def close(self) -> None:
            return None

    inner = _StreamingFake(
        [
            LLMStreamChunk(content_delta="he"),
            LLMStreamChunk(content_delta="llo"),
            LLMStreamChunk(finish_reason="stop"),
        ]
    )
    broadcaster = _RecordingBroadcaster()
    client = BroadcastingLLMClient(inner, broadcaster)
    ctx = RequestContext(
        tenant_id="t-1",
        principal_id="p-1",
        trace_id="trace-1",
        request_id="req-1",
        task_id="task-1",
    )

    with bind_context(ctx):
        response = await aggregate_stream_to_response(client, _request())

    # Graph-side: buffered response is shaped like ``complete`` returned it.
    assert response.content == "hello"
    assert response.finish_reason == "stop"
    # Wire-side: every chunk landed on the broadcaster channel with
    # the bound tenant_id + task_id from the request context.
    assert len(broadcaster.published) == 3
    assert all(t == "t-1" and tid == "task-1" for t, tid, _ in broadcaster.published)
    assert [c.content_delta for _, _, c in broadcaster.published] == ["he", "llo", ""]
    assert inner.stream_calls == 1


async def test_aggregator_synthetic_response_matches_complete_shape() -> None:
    """Streaming + aggregating produces the same observable shape as ``complete``.

    Documents the parity contract graphs rely on after migration.
    """

    # Build the "complete()-shape" response we want to match.
    expected = LLMResponse(
        content="hello world",
        model="openai/gpt-4o",
        finish_reason="stop",
        usage=LLMUsage(prompt_tokens=3, completion_tokens=5, total_tokens=8),
        tool_calls=(ToolCall(id="call_a", name="fs_read", arguments={"path": "a.py"}),),
        provider_response_id="resp-x",
    )
    # Now stream the same content via the aggregator.
    agg = StreamAggregator()
    agg.observe(LLMStreamChunk(content_delta="hello "))
    agg.observe(LLMStreamChunk(content_delta="world"))
    agg.observe(
        LLMStreamChunk(
            tool_call_deltas=(
                ToolCallDelta(index=0, id="call_a", name="fs_read", arguments_delta='{"path"'),
            )
        )
    )
    agg.observe(
        LLMStreamChunk(tool_call_deltas=(ToolCallDelta(index=0, arguments_delta=':"a.py"}'),))
    )
    agg.observe(
        LLMStreamChunk(
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=3, completion_tokens=5, total_tokens=8),
            model="openai/gpt-4o",
            provider_response_id="resp-x",
        )
    )
    actual = agg.to_response(_request())
    # Compare via JSON to avoid frozen-model equality fragility.
    assert json.loads(actual.model_dump_json()) == json.loads(expected.model_dump_json())
