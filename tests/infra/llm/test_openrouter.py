"""Unit tests for :class:`OpenRouterClient`.

The adapter is exercised against an :class:`httpx.MockTransport` so the
tests stay fast, deterministic, and never touch the network. A live
integration test against OpenRouter lives in
``tests/integration/test_openrouter_live.py`` and is skipped unless
``OPENROUTER_API_KEY`` is set.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from meta_agent.core.ports.llm import (
    ChatMessage,
    LLMAuthError,
    LLMInvalidRequestError,
    LLMRateLimitedError,
    LLMRequest,
    LLMStreamChunk,
    LLMTransientError,
    MessageRole,
)
from meta_agent.core.ports.tools import ToolCall, ToolCategory, ToolSpec
from meta_agent.infra.llm.config import OpenRouterConfig
from meta_agent.infra.llm.openrouter import OpenRouterClient


def _config(**overrides: object) -> OpenRouterConfig:
    base = {
        "api_key": "test-key",
        "default_model": "deepseek/deepseek-chat",
        "max_retries": 2,
        "initial_backoff_seconds": 0.01,
        "max_backoff_seconds": 0.05,
    }
    base.update(overrides)
    return OpenRouterConfig(**base)  # type: ignore[arg-type]


def _request() -> LLMRequest:
    return LLMRequest(
        messages=(ChatMessage(role=MessageRole.USER, content="hello"),),
        temperature=0.2,
        max_tokens=64,
    )


def _success_body(
    *, model: str = "deepseek/deepseek-chat", content: str = "hi"
) -> dict[str, object]:
    return {
        "id": "resp-1",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 2,
            "total_tokens": 7,
        },
    }


def _client(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    config: OpenRouterConfig | None = None,
    sleeps: list[float] | None = None,
) -> OpenRouterClient:
    transport = httpx.MockTransport(handler)
    captured: list[float] = sleeps if sleeps is not None else []

    async def fake_sleep(delay: float) -> None:
        captured.append(delay)

    return OpenRouterClient(config or _config(), transport=transport, sleep=fake_sleep)


async def test_complete_success_path_parses_response() -> None:
    received: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        received.append(req)
        return httpx.Response(200, json=_success_body(content="ok"))

    client = _client(handler)
    try:
        response = await client.complete(_request())
    finally:
        await client.close()

    assert response.content == "ok"
    assert response.model == "deepseek/deepseek-chat"
    assert response.finish_reason == "stop"
    assert response.usage.prompt_tokens == 5
    assert response.usage.total_tokens == 7
    assert response.provider_response_id == "resp-1"

    assert len(received) == 1
    req = received[0]
    assert req.url.path == "/api/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer test-key"
    body = req.content.decode()
    assert '"model":"deepseek/deepseek-chat"' in body
    assert '"temperature":0.2' in body


async def test_complete_retries_on_5xx_then_succeeds() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "upstream"})
        return httpx.Response(200, json=_success_body())

    client = _client(handler, sleeps=sleeps)
    try:
        response = await client.complete(_request())
    finally:
        await client.close()

    assert calls["n"] == 2
    assert response.content == "hi"
    assert sleeps == [0.01]  # exponential backoff first step


async def test_complete_429_uses_retry_after_then_succeeds() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "rate"}, headers={"retry-after": "0.03"})
        return httpx.Response(200, json=_success_body())

    client = _client(handler, sleeps=sleeps)
    try:
        await client.complete(_request())
    finally:
        await client.close()

    assert calls["n"] == 2
    assert sleeps == [0.03]


async def test_complete_gives_up_after_max_retries_on_5xx() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"error": "boom"})

    client = _client(handler, config=_config(max_retries=2))
    try:
        with pytest.raises(LLMTransientError):
            await client.complete(_request())
    finally:
        await client.close()
    assert calls["n"] == 3  # 1 initial + 2 retries


async def test_complete_does_not_retry_client_errors() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"error": "bad request"})

    client = _client(handler)
    try:
        with pytest.raises(LLMInvalidRequestError):
            await client.complete(_request())
    finally:
        await client.close()
    assert calls["n"] == 1


async def test_complete_auth_error_is_not_retried() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "unauthorized"})

    client = _client(handler)
    try:
        with pytest.raises(LLMAuthError):
            await client.complete(_request())
    finally:
        await client.close()
    assert calls["n"] == 1


async def test_complete_timeout_is_classified_transient_and_retried() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectTimeout("simulated timeout", request=req)
        return httpx.Response(200, json=_success_body())

    client = _client(handler, config=_config(max_retries=3))
    try:
        await client.complete(_request())
    finally:
        await client.close()
    assert calls["n"] == 3


async def test_complete_persistent_timeout_raises_transient_error() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated", request=req)

    client = _client(handler, config=_config(max_retries=1))
    try:
        with pytest.raises(LLMTransientError) as ei:
            await client.complete(_request())
    finally:
        await client.close()
    # Auth / RateLimited / InvalidRequest are NOT raised for transport faults.
    assert not isinstance(ei.value, LLMRateLimitedError)


async def test_complete_invalid_json_triggers_transient_retry() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                200, content=b"not json", headers={"content-type": "application/json"}
            )
        return httpx.Response(200, json=_success_body())

    client = _client(handler)
    try:
        response = await client.complete(_request())
    finally:
        await client.close()
    assert calls["n"] == 2
    assert response.content == "hi"


async def test_request_model_override_takes_precedence_over_config_default() -> None:
    captured: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req.content)
        return httpx.Response(200, json=_success_body(model="qwen/qwen3"))

    client = _client(handler)
    try:
        request = LLMRequest(
            messages=(ChatMessage(role=MessageRole.USER, content="x"),),
            model="qwen/qwen3",
        )
        response = await client.complete(request)
    finally:
        await client.close()
    assert response.model == "qwen/qwen3"
    assert b'"model":"qwen/qwen3"' in captured[0]


def test_config_from_env_requires_api_key() -> None:
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterConfig.from_env(env={}, required=True)


def test_config_from_env_reads_overrides() -> None:
    cfg = OpenRouterConfig.from_env(
        env={
            "OPENROUTER_API_KEY": "k",
            "OPENROUTER_BASE_URL": "https://example.test/v1/",
            "OPENROUTER_DEFAULT_MODEL": "qwen/qwen3",
            "OPENROUTER_MAX_RETRIES": "5",
        }
    )
    assert cfg.api_key == "k"
    assert cfg.base_url == "https://example.test/v1"
    assert cfg.default_model == "qwen/qwen3"
    assert cfg.max_retries == 5


def test_construct_rejects_empty_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        OpenRouterClient(_config(api_key=""))


def _tool_spec() -> ToolSpec:
    return ToolSpec(
        name="fs_read",
        description="read a file",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        category=ToolCategory.FILESYSTEM,
    )


async def test_request_serialises_tools_and_tool_messages() -> None:
    captured: list[bytes] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req.content)
        return httpx.Response(200, json=_success_body(content="done"))

    client = _client(handler)
    request = LLMRequest(
        messages=(
            ChatMessage(role=MessageRole.USER, content="please read"),
            ChatMessage(
                role=MessageRole.ASSISTANT,
                content="",
                tool_calls=(ToolCall(id="call_1", name="fs_read", arguments={"path": "x"}),),
            ),
            ChatMessage(
                role=MessageRole.TOOL,
                content="x-content",
                tool_call_id="call_1",
            ),
        ),
        tools=(_tool_spec(),),
    )
    try:
        await client.complete(request)
    finally:
        await client.close()

    body = captured[0].decode()
    assert '"tools"' in body
    assert '"function"' in body
    assert '"fs_read"' in body
    assert '"tool_calls"' in body
    assert '"tool_call_id":"call_1"' in body
    assert '"role":"tool"' in body


async def test_response_decodes_tool_calls_with_json_arguments() -> None:
    body = {
        "id": "resp-tools",
        "model": "deepseek/deepseek-chat",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc",
                            "type": "function",
                            "function": {
                                "name": "fs_read",
                                "arguments": '{"path": "foo.txt"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = _client(handler)
    try:
        response = await client.complete(_request())
    finally:
        await client.close()

    assert response.content == ""
    assert response.finish_reason == "tool_call"
    assert len(response.tool_calls) == 1
    call = response.tool_calls[0]
    assert call.id == "call_abc"
    assert call.name == "fs_read"
    assert call.arguments == {"path": "foo.txt"}


async def test_response_with_invalid_tool_call_json_args_raises_transient() -> None:
    body = {
        "id": "r",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c",
                            "type": "function",
                            "function": {"name": "fs_read", "arguments": "not json"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = _client(handler, config=_config(max_retries=0))
    try:
        with pytest.raises(LLMTransientError):
            await client.complete(_request())
    finally:
        await client.close()


async def test_response_with_null_content_and_no_tool_calls_raises_transient() -> None:
    body = {
        "id": "r",
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": None},
                "finish_reason": "stop",
            }
        ],
    }

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = _client(handler, config=_config(max_retries=0))
    try:
        with pytest.raises(LLMTransientError):
            await client.complete(_request())
    finally:
        await client.close()


# --------------------------------------------------------------------- streaming


def _sse_bytes(*events: str) -> bytes:
    """Render an OpenAI-compatible SSE response body from raw event payloads."""
    return ("".join(f"data: {ev}\n\n" for ev in events)).encode("utf-8")


async def _drain_stream(client: OpenRouterClient) -> list[LLMStreamChunk]:
    chunks: list[LLMStreamChunk] = []
    async for chunk in client.stream(_request()):
        chunks.append(chunk)
    return chunks


async def test_stream_emits_content_deltas_then_terminal_chunk() -> None:
    received: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        received.append(req)
        return httpx.Response(
            200,
            content=_sse_bytes(
                '{"id":"resp-1","model":"deepseek/deepseek-chat",'
                '"choices":[{"index":0,"delta":{"content":"hel"}}]}',
                '{"choices":[{"index":0,"delta":{"content":"lo"}}]}',
                '{"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
                '"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}',
                "[DONE]",
            ),
            headers={"content-type": "text/event-stream"},
        )

    client = _client(handler)
    try:
        chunks = await _drain_stream(client)
    finally:
        await client.close()

    body = received[0].content.decode()
    assert '"stream":true' in body
    contents = [c.content_delta for c in chunks if c.content_delta]
    assert "".join(contents) == "hello"
    terminal = chunks[-1]
    assert terminal.finish_reason == "stop"
    assert terminal.usage is not None
    assert terminal.usage.total_tokens == 7


async def test_stream_assembles_tool_call_deltas_with_partial_arguments() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse_bytes(
                '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
                '"id":"call_1","function":{"name":"fs_read","arguments":"{\\"pa"}}]}}]}',
                '{"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
                '"function":{"arguments":"th\\":\\"a.py\\"}"}}]}}]}',
                '{"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
                "[DONE]",
            ),
            headers={"content-type": "text/event-stream"},
        )

    client = _client(handler)
    try:
        chunks = await _drain_stream(client)
    finally:
        await client.close()

    deltas = [d for c in chunks for d in c.tool_call_deltas]
    assert [d.index for d in deltas] == [0, 0]
    assert deltas[0].id == "call_1"
    assert deltas[0].name == "fs_read"
    assert deltas[0].arguments_delta == '{"pa'
    assert deltas[1].id is None
    assert deltas[1].arguments_delta == 'th":"a.py"}'
    assert chunks[-1].finish_reason == "tool_call"


async def test_stream_skips_non_data_lines_and_heartbeats() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        body = (
            b": openrouter keepalive\n\n"
            b"event: ping\ndata: {}\n\n"
            b'data: {"choices":[{"index":0,"delta":{"content":"a"}}]}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    client = _client(handler)
    try:
        chunks = await _drain_stream(client)
    finally:
        await client.close()

    contents = [c.content_delta for c in chunks if c.content_delta]
    assert contents == ["a"]


async def test_stream_raises_invalid_request_before_any_chunk_on_4xx() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "bad request"})

    client = _client(handler, config=_config(max_retries=0))
    try:
        with pytest.raises(LLMInvalidRequestError):
            await _drain_stream(client)
    finally:
        await client.close()


async def test_stream_raises_auth_error_without_retry() -> None:
    calls = {"n": 0}

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "auth"})

    client = _client(handler, config=_config(max_retries=2))
    try:
        with pytest.raises(LLMAuthError):
            await _drain_stream(client)
    finally:
        await client.close()
    assert calls["n"] == 1


async def test_stream_retries_on_5xx_then_streams_successfully() -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    def handler(_req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"error": "upstream"})
        return httpx.Response(
            200,
            content=_sse_bytes(
                '{"choices":[{"index":0,"delta":{"content":"ok"}}]}',
                '{"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}',
                "[DONE]",
            ),
            headers={"content-type": "text/event-stream"},
        )

    client = _client(handler, sleeps=sleeps)
    try:
        chunks = await _drain_stream(client)
    finally:
        await client.close()
    assert calls["n"] == 2
    assert sleeps == [0.01]
    assert "".join(c.content_delta for c in chunks) == "ok"


async def test_stream_raises_rate_limited_with_retry_after() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate"}, headers={"retry-after": "0.05"})

    client = _client(handler, config=_config(max_retries=0))
    try:
        with pytest.raises(LLMRateLimitedError) as excinfo:
            await _drain_stream(client)
    finally:
        await client.close()
    assert excinfo.value.retry_after == 0.05


async def test_stream_malformed_json_in_event_raises_transient() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"data: {not valid json\n\n",
            headers={"content-type": "text/event-stream"},
        )

    client = _client(handler, config=_config(max_retries=0))
    try:
        with pytest.raises(LLMTransientError):
            await _drain_stream(client)
    finally:
        await client.close()
