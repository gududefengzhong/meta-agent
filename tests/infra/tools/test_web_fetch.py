"""Unit tests for :class:`HttpxWebFetchTool`.

Uses ``httpx.MockTransport`` so no real network IO ever fires — the
mock answers each request with whatever the test scripted.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from meta_agent.core.ports.tools import (
    ToolContext,
    ToolExecutionError,
    ToolPermissionError,
    ToolValidationError,
)
from meta_agent.infra.tools.web_fetch import HttpxWebFetchTool

Handler = Callable[[httpx.Request], httpx.Response]


def _ctx(*, output_byte_cap: int = 64_000) -> ToolContext:
    return ToolContext(
        tenant_id="t-1",
        task_id="task-1",
        trace_id="trace-1",
        workspace_path=None,
        output_byte_cap=output_byte_cap,
    )


def _tool_with(
    handler: Handler,
    *,
    allowed_hosts: frozenset[str] = frozenset({"example.com"}),
) -> HttpxWebFetchTool:
    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return HttpxWebFetchTool(allowed_hosts=allowed_hosts, client_factory=factory)


async def test_happy_path_returns_decoded_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"hello world",
            headers={"content-type": "text/plain; charset=utf-8"},
        )

    tool = _tool_with(handler)
    outcome = await tool.fetch(_ctx(), url="https://example.com/page")
    assert outcome.status == 200
    assert outcome.content == "hello world"
    assert outcome.content_type == "text/plain"
    assert outcome.bytes_received == len(b"hello world")
    assert outcome.truncated is False


async def test_suffix_match_accepts_subdomain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"sub", headers={"content-type": "text/plain"})

    tool = _tool_with(handler, allowed_hosts=frozenset({"example.com"}))
    outcome = await tool.fetch(_ctx(), url="https://docs.example.com/foo")
    assert outcome.status == 200


async def test_suffix_match_rejects_lookalike_hostname() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        raise AssertionError("request should not have fired")

    tool = _tool_with(handler, allowed_hosts=frozenset({"example.com"}))
    with pytest.raises(ToolPermissionError):
        await tool.fetch(_ctx(), url="https://evilexample.com/x")


async def test_hostname_not_in_allowlist_raises_permission_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("request should not have fired")

    tool = _tool_with(handler, allowed_hosts=frozenset({"example.com"}))
    with pytest.raises(ToolPermissionError):
        await tool.fetch(_ctx(), url="https://other.com/")


async def test_non_http_scheme_rejected_before_network() -> None:
    tool = _tool_with(lambda req: httpx.Response(200))
    with pytest.raises(ToolValidationError, match="scheme"):
        await tool.fetch(_ctx(), url="file:///etc/passwd")
    with pytest.raises(ToolValidationError, match="scheme"):
        await tool.fetch(_ctx(), url="ssh://example.com/")


async def test_blank_url_rejected() -> None:
    tool = _tool_with(lambda req: httpx.Response(200))
    with pytest.raises(ToolValidationError):
        await tool.fetch(_ctx(), url="")


async def test_binary_content_type_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"\x00\x01\x02",
            headers={"content-type": "application/octet-stream"},
        )

    tool = _tool_with(handler)
    with pytest.raises(ToolValidationError, match="content-type"):
        await tool.fetch(_ctx(), url="https://example.com/blob")


async def test_size_cap_marks_truncated_and_drops_overflow() -> None:
    body = b"x" * 200

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/plain"})

    tool = _tool_with(handler)
    outcome = await tool.fetch(_ctx(output_byte_cap=50), url="https://example.com/big")
    assert outcome.truncated is True
    assert outcome.bytes_received == 200
    assert len(outcome.content) == 50


async def test_non_2xx_response_returned_without_raising() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"missing", headers={"content-type": "text/plain"})

    tool = _tool_with(handler)
    outcome = await tool.fetch(_ctx(), url="https://example.com/missing")
    assert outcome.status == 404
    assert outcome.content == "missing"


async def test_timeout_raises_tool_execution_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated read timeout", request=request)

    tool = _tool_with(handler)
    with pytest.raises(ToolExecutionError, match="timed out"):
        await tool.fetch(_ctx(), url="https://example.com/slow")


async def test_network_error_raises_tool_execution_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    tool = _tool_with(handler)
    with pytest.raises(ToolExecutionError, match="network error"):
        await tool.fetch(_ctx(), url="https://example.com/down")


async def test_constructor_rejects_empty_allow_list() -> None:
    with pytest.raises(ValueError, match="allowed_hosts"):
        HttpxWebFetchTool(allowed_hosts=frozenset())


async def test_close_is_idempotent() -> None:
    tool = _tool_with(lambda req: httpx.Response(200))
    await tool.close()
    await tool.close()  # second call must not raise
