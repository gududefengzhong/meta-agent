"""Unit tests for :class:`BroadcastingLLMClient`."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from meta_agent.core.ports.chunk_broadcaster import (
    ChunkBroadcaster,
    ChunkBroadcasterError,
)
from meta_agent.core.ports.llm import (
    ChatMessage,
    LLMClient,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    MessageRole,
)
from meta_agent.infra.llm.broadcasting import BroadcastingLLMClient
from meta_agent.infra.security.context import RequestContext, bind_context


class _FakeBroadcaster(ChunkBroadcaster):
    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[tuple[str, str, LLMStreamChunk]] = []
        self._fail = fail

    async def publish(self, *, tenant_id: str, task_id: str, chunk: LLMStreamChunk) -> None:
        if self._fail:
            raise ChunkBroadcasterError("redis blip")
        self.published.append((tenant_id, task_id, chunk))

    async def subscribe(self, *, tenant_id: str, task_id: str):  # type: ignore[no-untyped-def]
        raise AssertionError("subscribe not exercised by decorator tests")

    async def close(self) -> None:
        return None


class _StreamingFake(LLMClient):
    def __init__(self, chunks: list[LLMStreamChunk]) -> None:
        self._chunks = chunks

    async def complete(self, request: LLMRequest) -> LLMResponse:
        raise AssertionError("complete not used")

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        for chunk in self._chunks:
            yield chunk

    async def close(self) -> None:
        return None


def _ctx(task_id: str | None = "task-1") -> RequestContext:
    return RequestContext(
        tenant_id="t-1",
        principal_id="p-1",
        trace_id="trace-1",
        request_id="req-1",
        task_id=task_id,
    )


def _request() -> LLMRequest:
    return LLMRequest(messages=(ChatMessage(role=MessageRole.USER, content="hi"),))


async def _drain(client: LLMClient) -> list[LLMStreamChunk]:
    chunks: list[LLMStreamChunk] = []
    async for chunk in client.stream(_request()):
        chunks.append(chunk)
    return chunks


async def test_stream_publishes_each_chunk_with_tenant_and_task_id() -> None:
    inner = _StreamingFake(
        [
            LLMStreamChunk(content_delta="he"),
            LLMStreamChunk(content_delta="llo"),
            LLMStreamChunk(finish_reason="stop"),
        ]
    )
    broadcaster = _FakeBroadcaster()
    client = BroadcastingLLMClient(inner, broadcaster)
    with bind_context(_ctx()):
        emitted = await _drain(client)
    assert [c.content_delta for c in emitted] == ["he", "llo", ""]
    assert len(broadcaster.published) == 3
    assert all(t == "t-1" and tid == "task-1" for t, tid, _ in broadcaster.published)


async def test_stream_without_task_id_skips_publish() -> None:
    inner = _StreamingFake([LLMStreamChunk(content_delta="x")])
    broadcaster = _FakeBroadcaster()
    client = BroadcastingLLMClient(inner, broadcaster)
    with bind_context(_ctx(task_id=None)):
        emitted = await _drain(client)
    assert emitted and emitted[0].content_delta == "x"
    assert broadcaster.published == []


async def test_stream_without_bound_context_skips_publish() -> None:
    inner = _StreamingFake([LLMStreamChunk(content_delta="x")])
    broadcaster = _FakeBroadcaster()
    client = BroadcastingLLMClient(inner, broadcaster)
    emitted = await _drain(client)
    assert emitted and emitted[0].content_delta == "x"
    assert broadcaster.published == []


async def test_publish_error_is_logged_and_swallowed(
    caplog: object,
) -> None:
    inner = _StreamingFake([LLMStreamChunk(content_delta="a"), LLMStreamChunk(content_delta="b")])
    broadcaster = _FakeBroadcaster(fail=True)
    client = BroadcastingLLMClient(inner, broadcaster)
    # The agent loop must NOT be stalled by a broadcaster failure.
    with bind_context(_ctx()), _capture_logs() as records:
        emitted = await _drain(client)
    assert [c.content_delta for c in emitted] == ["a", "b"]
    publish_failed = [r for r in records if r.message == "llm.broadcast.publish_failed"]
    # Two chunks attempted, two failures logged.
    assert len(publish_failed) == 2


async def test_complete_is_pass_through_no_broadcast() -> None:
    class _CompleteFake(LLMClient):
        def __init__(self) -> None:
            self.complete_calls = 0

        async def complete(self, request: LLMRequest) -> LLMResponse:
            self.complete_calls += 1
            return LLMResponse(content="done", model="m", finish_reason="stop")

        async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
            if False:
                yield LLMStreamChunk()
            raise AssertionError("stream not used")

        async def close(self) -> None:
            return None

    inner = _CompleteFake()
    broadcaster = _FakeBroadcaster()
    client = BroadcastingLLMClient(inner, broadcaster)
    with bind_context(_ctx()):
        response = await client.complete(_request())
    assert response.content == "done"
    assert inner.complete_calls == 1
    assert broadcaster.published == []


# ---------------------------------------------------------------- helpers


class _LogCapture:
    def __init__(self, name: str = "meta_agent.infra.llm.broadcasting") -> None:
        self._name = name
        self.records: list[logging.LogRecord] = []
        self._handler = _ListHandler(self.records)
        self._logger = logging.getLogger(name)
        self._prev_level: int | None = None

    def __enter__(self) -> list[logging.LogRecord]:
        self._prev_level = self._logger.level
        self._logger.setLevel(logging.DEBUG)
        self._logger.addHandler(self._handler)
        return self.records

    def __exit__(self, *exc_info: object) -> None:
        self._logger.removeHandler(self._handler)
        if self._prev_level is not None:
            self._logger.setLevel(self._prev_level)


class _ListHandler(logging.Handler):
    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__(level=logging.DEBUG)
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record)


def _capture_logs() -> _LogCapture:
    return _LogCapture()
