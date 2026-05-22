"""Handler tests for the WEB-category tools (``web_fetch`` / ``doc_search``).

Focuses on the argument plumbing and ``ToolResult`` shape — the
underlying typed methods are covered by ``test_web_fetch`` /
``test_doc_search``.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from meta_agent.core.capabilities.executor import ToolExecutor
from meta_agent.core.capabilities.registry import ToolRegistry
from meta_agent.core.ports.tools import (
    ToolCall,
    ToolContext,
    ToolValidationError,
)
from meta_agent.infra.tools.doc_search import DocEntry, InMemoryDocSearchTool
from meta_agent.infra.tools.local_handlers import (
    TOOL_DOC_SEARCH,
    TOOL_WEB_FETCH,
    register_local_workspace_tools,
)
from meta_agent.infra.tools.local_workspace import (
    LocalWorkspaceEditTool,
    LocalWorkspaceFileSystemTool,
    LocalWorkspaceShellTool,
    LocalWorkspaceTestTool,
)
from meta_agent.infra.tools.web_fetch import HttpxWebFetchTool

Handler = Callable[[httpx.Request], httpx.Response]


def _ctx() -> ToolContext:
    return ToolContext(
        tenant_id="t",
        task_id="task",
        trace_id="trace",
        workspace_path=None,
        output_byte_cap=64_000,
    )


def _web_tool(handler: Handler) -> HttpxWebFetchTool:
    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return HttpxWebFetchTool(allowed_hosts=frozenset({"example.com"}), client_factory=factory)


def _registry_with_web(
    web: HttpxWebFetchTool | None = None,
    *,
    doc: InMemoryDocSearchTool | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    register_local_workspace_tools(
        registry,
        fs=LocalWorkspaceFileSystemTool(),
        edit=LocalWorkspaceEditTool(),
        shell=LocalWorkspaceShellTool(allowed_commands=frozenset({"python"})),
        test=LocalWorkspaceTestTool(),
        web_fetch=web,
        doc_search=doc,
    )
    return registry


def test_web_and_doc_tools_register_when_provided() -> None:
    web = _web_tool(lambda req: httpx.Response(200))
    doc = InMemoryDocSearchTool((DocEntry(source_uri="d", title="t", body="b"),))
    registry = _registry_with_web(web=web, doc=doc)
    assert TOOL_WEB_FETCH in registry.names()
    assert TOOL_DOC_SEARCH in registry.names()


def test_web_and_doc_tools_omitted_when_not_provided() -> None:
    registry = _registry_with_web()
    assert TOOL_WEB_FETCH not in registry.names()
    assert TOOL_DOC_SEARCH not in registry.names()


async def test_web_fetch_handler_renders_body_and_metadata() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"hi from example",
            headers={"content-type": "text/plain"},
        )

    registry = _registry_with_web(web=_web_tool(handler))
    executor = ToolExecutor(registry)
    result = await executor.execute(
        ToolCall(
            id="c1",
            name=TOOL_WEB_FETCH,
            arguments={"url": "https://example.com/index"},
        ),
        _ctx(),
    )
    assert result.is_error is False
    assert "status=200" in result.content
    assert "content_type=text/plain" in result.content
    assert "hi from example" in result.content
    assert result.metadata == {
        "status": "200",
        "content_type": "text/plain",
        "bytes_received": str(len(b"hi from example")),
    }


async def test_web_fetch_handler_marks_non_2xx_as_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"down", headers={"content-type": "text/plain"})

    registry = _registry_with_web(web=_web_tool(handler))
    executor = ToolExecutor(registry)
    result = await executor.execute(
        ToolCall(
            id="c2",
            name=TOOL_WEB_FETCH,
            arguments={"url": "https://example.com/health"},
        ),
        _ctx(),
    )
    assert result.is_error is True
    assert "status=503" in result.content


async def test_web_fetch_handler_rejects_non_numeric_timeout() -> None:
    registry = _registry_with_web(web=_web_tool(lambda req: httpx.Response(200)))
    handler = registry.get(TOOL_WEB_FETCH).handler
    with pytest.raises(ToolValidationError):
        await handler(
            ToolCall(
                id="c3",
                name=TOOL_WEB_FETCH,
                arguments={"url": "https://example.com/", "timeout_seconds": "soon"},
            ),
            _ctx(),
        )


async def test_doc_search_handler_renders_hits_with_uri_and_snippet() -> None:
    corpus = (
        DocEntry(source_uri="doc://infra/redis", title="Redis", body="redis-cli ping"),
        DocEntry(source_uri="doc://infra/postgres", title="Postgres", body="psql shell"),
    )
    registry = _registry_with_web(doc=InMemoryDocSearchTool(corpus))
    executor = ToolExecutor(registry)
    result = await executor.execute(
        ToolCall(
            id="c4",
            name=TOOL_DOC_SEARCH,
            arguments={"query": "redis-cli", "limit": 2},
        ),
        _ctx(),
    )
    assert result.metadata.get("hits") == "1"
    assert "doc://infra/redis" in result.content
    assert "redis-cli" in result.content.lower()


async def test_doc_search_handler_empty_result_is_explicit() -> None:
    corpus = (DocEntry(source_uri="doc://x", title="t", body="nothing here"),)
    registry = _registry_with_web(doc=InMemoryDocSearchTool(corpus))
    executor = ToolExecutor(registry)
    result = await executor.execute(
        ToolCall(
            id="c5",
            name=TOOL_DOC_SEARCH,
            arguments={"query": "quantum"},
        ),
        _ctx(),
    )
    assert result.metadata.get("hits") == "0"
    assert "no documents matched" in result.content
