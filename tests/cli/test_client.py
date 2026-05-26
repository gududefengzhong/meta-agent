"""Unit tests for the CLI HTTP client.

Drives :class:`TaskClient` against ``httpx.MockTransport`` so the
SSE / submit / get paths can be exercised without any sockets.
"""

from __future__ import annotations

from collections.abc import Iterable

import httpx
import pytest

from meta_agent.cli.client import (
    EXIT_NETWORK,
    EXIT_USAGE,
    CLIConfig,
    CLIError,
    TaskClient,
)


def _config(token: str = "tok-test") -> CLIConfig:
    return CLIConfig(api_url="http://test", token=token)


def _client(handler) -> TaskClient:  # type: ignore[no-untyped-def]
    transport = httpx.MockTransport(handler)
    return TaskClient(_config(), transport=transport)


def _sse_body(events: Iterable[str]) -> bytes:
    return ("".join(f"data: {e}\n\n" for e in events)).encode("utf-8")


async def test_config_from_env_requires_token() -> None:
    with pytest.raises(CLIError) as excinfo:
        CLIConfig.from_env(env={})
    assert excinfo.value.exit_code == EXIT_USAGE
    assert "missing bearer token" in excinfo.value.message


async def test_config_flag_overrides_env() -> None:
    cfg = CLIConfig.from_env(
        api_url="http://flagged",
        token="tok-flag",
        env={"META_AGENT_API_URL": "http://env", "META_AGENT_TOKEN": "tok-env"},
    )
    assert cfg.api_url == "http://flagged"
    assert cfg.token == "tok-flag"


async def test_config_env_used_when_flags_absent() -> None:
    cfg = CLIConfig.from_env(
        env={"META_AGENT_API_URL": "http://env/", "META_AGENT_TOKEN": "tok-env"}
    )
    assert cfg.api_url == "http://env"  # trailing slash stripped
    assert cfg.token == "tok-env"


async def test_submit_task_posts_with_bearer_and_returns_response() -> None:
    received: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        received.append(req)
        return httpx.Response(
            201,
            json={
                "task_id": "t-1",
                "tenant_id": "ten-1",
                "state": "pending",
                "task_type": "system_shell_agent",
                "trace_id": "tr-1",
                "session_id": None,
                "permission_mode": "auto",
                "budget_policy": "none",
                "created_at": "2026-06-23T00:00:00+00:00",
                "updated_at": "2026-06-23T00:00:00+00:00",
            },
        )

    async with _client(handler) as client:
        task = await client.submit_task(
            task_type="system_shell_agent",
            input_payload={"user_prompt": "hello"},
        )

    assert task["task_id"] == "t-1"
    req = received[0]
    assert req.url.path == "/v1/tasks"
    assert req.headers["authorization"] == "Bearer tok-test"
    assert b'"user_prompt":"hello"' in req.content


async def test_submit_task_4xx_raises_cli_error_with_usage_exit_code() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "task_type invalid"})

    async with _client(handler) as client:
        with pytest.raises(CLIError) as excinfo:
            await client.submit_task(task_type="bogus", input_payload={"user_prompt": "x"})
    assert excinfo.value.exit_code == EXIT_USAGE
    assert "HTTP 400" in excinfo.value.message
    assert "task_type invalid" in excinfo.value.message


async def test_submit_task_5xx_raises_cli_error_with_network_exit_code() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    async with _client(handler) as client:
        with pytest.raises(CLIError) as excinfo:
            await client.submit_task(
                task_type="system_shell_agent", input_payload={"user_prompt": "x"}
            )
    assert excinfo.value.exit_code == EXIT_NETWORK


async def test_get_trajectory_fetches_task_timeline_with_limit() -> None:
    received: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        received.append(req)
        return httpx.Response(200, json={"items": [], "truncated": False})

    async with _client(handler) as client:
        body = await client.get_trajectory("t-1", limit_per_source=42)

    assert body == {"items": [], "truncated": False}
    req = received[0]
    assert req.url.path == "/v1/tasks/t-1/trajectory"
    assert req.url.params["limit_per_source"] == "42"


async def test_stream_llm_chunks_yields_parsed_payloads() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse_body(
                [
                    '{"content_delta":"he"}',
                    '{"content_delta":"llo"}',
                    '{"finish_reason":"stop"}',
                    "[DONE]",
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    chunks = []
    async with _client(handler) as client:
        async for chunk in client.stream_llm_chunks("t-1"):
            chunks.append(chunk)

    assert [c.get("content_delta") for c in chunks if "content_delta" in c] == ["he", "llo"]
    assert chunks[-1].get("finish_reason") == "stop"


async def test_stream_events_yields_parsed_payloads() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_sse_body(
                [
                    '{"event_id":"e-1","action":"task.node_started"}',
                    '{"state":"succeeded"}',
                ]
            ),
            headers={"content-type": "text/event-stream"},
        )

    events = []
    async with _client(handler) as client:
        async for ev in client.stream_events("t-1"):
            events.append(ev)

    actions = [e.get("action") for e in events if "action" in e]
    states = [e.get("state") for e in events if "state" in e]
    assert actions == ["task.node_started"]
    assert states == ["succeeded"]


async def test_stream_llm_chunks_4xx_raises_before_yielding() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "task not found"})

    async with _client(handler) as client:
        with pytest.raises(CLIError) as excinfo:
            async for _ in client.stream_llm_chunks("missing"):
                pass
    assert excinfo.value.exit_code == EXIT_USAGE
    assert "HTTP 404" in excinfo.value.message


async def test_stream_skips_non_data_lines_and_malformed_json() -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        body = (
            b": heartbeat\n\n"
            b"event: ping\n"
            b'data: {"content_delta":"a"}\n\n'
            b"data: not valid json\n\n"
            b'data: {"content_delta":"b"}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    chunks = []
    async with _client(handler) as client:
        async for chunk in client.stream_llm_chunks("t-1"):
            chunks.append(chunk)
    assert [c.get("content_delta") for c in chunks] == ["a", "b"]
