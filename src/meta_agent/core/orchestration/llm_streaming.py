"""Aggregate :class:`LLMClient.stream` output into an :class:`LLMResponse`.

Why a helper
============
Phase δ-1 introduced :class:`BroadcastingLLMClient` (outermost in
the LLM stack). It only fans chunks out when graphs invoke
:meth:`LLMClient.stream` — :meth:`LLMClient.complete` is a buffered
one-shot and never produces a chunk for the SSE wire.

Graphs, however, work in terms of :class:`LLMResponse` (synchronous
result with ``content`` / ``tool_calls`` / ``usage`` / ``finish_reason``).
Rewriting every graph node to consume an async iterator would be a
much larger change than δ-1 needs. Instead, this helper:

1. Calls ``llm.stream(request)``
2. Lets chunks flow through the production decorator stack —
   :class:`BroadcastingLLMClient` publishes them to the per-task
   channel as a side effect, so the SSE endpoint sees real-time
   tokens
3. Aggregates the chunks into a synthetic :class:`LLMResponse`
   shaped identically to what ``complete()`` would have returned
4. Hands the response back to the graph node, which proceeds
   exactly as before

Semantics
=========
The synthetic response MUST be functionally equivalent to
``complete()`` for the same request — graphs don't observe whether
the result was buffered or streamed. ``finish_reason`` defaults to
``"other"`` if the upstream never emitted one (broken provider);
``usage`` defaults to an empty :class:`LLMUsage` so metering rows
still write cleanly.

Tool-call argument JSON is parsed best-effort: a malformed
``arguments`` string lands as an empty dict in the synthetic
response rather than raising, because the graph nodes downstream
treat tool-call arguments defensively anyway. This matches
:class:`MeteredLLMClient`'s own internal aggregator behaviour.
"""

from __future__ import annotations

import json
from typing import Any

from meta_agent.core.ports.llm import (
    FinishReason,
    LLMClient,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    LLMUsage,
)
from meta_agent.core.ports.tools import ToolCall


class StreamAggregator:
    """Accumulate :class:`LLMStreamChunk` instances into one terminal response.

    ``content_delta`` parts concatenate left-to-right; tool-call
    deltas merge per ``index`` (``id`` / ``name`` kept from whichever
    chunk supplies them first, ``arguments_delta`` parts
    concatenate). The synthetic :class:`LLMResponse` produced by
    :meth:`to_response` mirrors what a non-streaming call would
    return for the same upstream interaction.
    """

    def __init__(self) -> None:
        self._content_parts: list[str] = []
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._finish_reason: FinishReason | None = None
        self._usage: LLMUsage | None = None
        self._model: str | None = None
        self._provider_response_id: str | None = None

    def observe(self, chunk: LLMStreamChunk) -> None:
        if chunk.content_delta:
            self._content_parts.append(chunk.content_delta)
        for delta in chunk.tool_call_deltas:
            entry = self._tool_calls.setdefault(
                delta.index,
                {"id": None, "name": None, "arguments_parts": []},
            )
            if delta.id is not None:
                entry["id"] = delta.id
            if delta.name is not None:
                entry["name"] = delta.name
            if delta.arguments_delta:
                entry["arguments_parts"].append(delta.arguments_delta)
        if chunk.finish_reason is not None:
            self._finish_reason = chunk.finish_reason
        if chunk.usage is not None:
            self._usage = chunk.usage
        if chunk.model is not None:
            self._model = chunk.model
        if chunk.provider_response_id is not None:
            self._provider_response_id = chunk.provider_response_id

    def to_response(self, request: LLMRequest) -> LLMResponse:
        tool_calls: list[ToolCall] = []
        for index in sorted(self._tool_calls):
            entry = self._tool_calls[index]
            call_id = entry["id"]
            name = entry["name"]
            if not isinstance(call_id, str) or not isinstance(name, str):
                continue
            args_str = "".join(entry["arguments_parts"])
            arguments: dict[str, Any]
            if not args_str:
                arguments = {}
            else:
                try:
                    decoded = json.loads(args_str)
                except ValueError:
                    arguments = {}
                else:
                    arguments = decoded if isinstance(decoded, dict) else {}
            tool_calls.append(ToolCall(id=call_id, name=name, arguments=arguments))
        return LLMResponse(
            content="".join(self._content_parts),
            model=self._model if self._model is not None else (request.model or ""),
            finish_reason=self._finish_reason if self._finish_reason is not None else "other",
            usage=self._usage if self._usage is not None else LLMUsage(),
            tool_calls=tuple(tool_calls),
            provider_response_id=self._provider_response_id,
        )


async def aggregate_stream_to_response(llm: LLMClient, request: LLMRequest) -> LLMResponse:
    """Drive ``llm.stream(request)`` and return a synthetic :class:`LLMResponse`.

    Use this in graph nodes instead of :meth:`LLMClient.complete` so
    chunks flow through the production stack (broadcaster /
    metering / redaction) as they arrive — the SSE wire to the
    client sees real-time tokens while the node still gets a
    buffered response.

    The aggregation is forward-only (no rewind / replay). If the
    underlying stream raises, the exception propagates unchanged —
    callers get the same error taxonomy as ``complete()``.
    """

    aggregator = StreamAggregator()
    async for chunk in llm.stream(request):
        aggregator.observe(chunk)
    return aggregator.to_response(request)


__all__ = ["StreamAggregator", "aggregate_stream_to_response"]
