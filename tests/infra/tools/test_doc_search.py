"""Unit tests for :class:`InMemoryDocSearchTool`."""

from __future__ import annotations

import pytest

from meta_agent.core.ports.tools import ToolContext, ToolValidationError
from meta_agent.infra.tools.doc_search import DocEntry, InMemoryDocSearchTool


def _ctx() -> ToolContext:
    return ToolContext(
        tenant_id="t-1",
        task_id="task-1",
        trace_id="trace-1",
        workspace_path=None,
        output_byte_cap=65_536,
    )


_CORPUS = (
    DocEntry(
        source_uri="doc://infra/postgres",
        title="Postgres deployment runbook",
        body=(
            "The production cluster runs Postgres 16. To rotate the read replica, "
            "follow the failover checklist."
        ),
    ),
    DocEntry(
        source_uri="doc://infra/redis",
        title="Redis runbook",
        body="Use redis-cli ping to confirm the primary is reachable.",
    ),
    DocEntry(
        source_uri="doc://onboarding",
        title="New engineer onboarding",
        body="Set up tools, pair with a buddy, and ship a small fix.",
    ),
)


async def test_search_returns_ranked_hits() -> None:
    search = InMemoryDocSearchTool(_CORPUS)
    hits = await search.search(_ctx(), query="postgres replica")
    assert len(hits) >= 1
    assert hits[0].source_uri == "doc://infra/postgres"
    # All hits report a score in (0, 1].
    assert all(0.0 < hit.score <= 1.0 for hit in hits)


async def test_search_returns_empty_when_no_token_matches() -> None:
    search = InMemoryDocSearchTool(_CORPUS)
    hits = await search.search(_ctx(), query="quantum entanglement")
    assert hits == ()


async def test_limit_clamped_and_respected() -> None:
    search = InMemoryDocSearchTool(_CORPUS)
    hits = await search.search(_ctx(), query="runbook", limit=1)
    assert len(hits) == 1
    # Internally _MAX_LIMIT (20) is respected — limit=999 yields at most #corpus hits.
    big = await search.search(_ctx(), query="the", limit=999)
    assert len(big) <= len(_CORPUS)


async def test_blank_query_rejected() -> None:
    search = InMemoryDocSearchTool(_CORPUS)
    with pytest.raises(ToolValidationError):
        await search.search(_ctx(), query="")
    with pytest.raises(ToolValidationError):
        await search.search(_ctx(), query="   ")


async def test_non_positive_limit_rejected() -> None:
    search = InMemoryDocSearchTool(_CORPUS)
    with pytest.raises(ToolValidationError):
        await search.search(_ctx(), query="redis", limit=0)
    with pytest.raises(ToolValidationError):
        await search.search(_ctx(), query="redis", limit=-1)


async def test_snippet_includes_query_term_when_present_in_body() -> None:
    search = InMemoryDocSearchTool(_CORPUS)
    hits = await search.search(_ctx(), query="redis-cli")
    assert len(hits) == 1
    assert "redis-cli" in hits[0].snippet.lower()


async def test_snippet_falls_back_to_body_start_when_match_only_in_title() -> None:
    corpus = (
        DocEntry(
            source_uri="doc://x",
            title="kubernetes overview",
            body="This document explains how clusters are organised.",
        ),
    )
    search = InMemoryDocSearchTool(corpus)
    hits = await search.search(_ctx(), query="kubernetes")
    assert len(hits) == 1
    # Body chunk starts at position 0 since no body token matched.
    assert hits[0].snippet.startswith("This document")


async def test_ties_resolved_by_insertion_order() -> None:
    corpus = (
        DocEntry(source_uri="doc://first", title="alpha", body="alpha topic"),
        DocEntry(source_uri="doc://second", title="alpha", body="alpha topic"),
    )
    search = InMemoryDocSearchTool(corpus)
    hits = await search.search(_ctx(), query="alpha topic")
    assert hits[0].source_uri == "doc://first"
    assert hits[1].source_uri == "doc://second"


async def test_constructor_rejects_empty_source_uri() -> None:
    with pytest.raises(ValueError, match="source_uri"):
        InMemoryDocSearchTool((DocEntry(source_uri="", title="t", body="b"),))
