"""In-memory :class:`DocSearchTool` for the Phase β+ default knowledge surface.

This adapter is the v0 default — it loads a small fixed corpus at
construction time (project READMEs, runbook snippets, internal HOWTOs)
and answers ``search(query)`` via keyword overlap scoring. Future
adapters (OSS / COS / vendor doc APIs) implement the same Port and
swap in without touching graph code.

Scoring (deliberately dumb, deliberately predictable):

* Tokenize ``query`` and each document's ``title + body`` into
  lowercased word-character runs.
* Score = (#query-tokens that appear in the document, counted once
  each) / (#unique query tokens).
* Tie-break by document insertion order (stable across calls).
* Snippet = first ~240 chars surrounding the earliest matching
  token in the document body.

Why not BM25 / embeddings: the in-memory adapter exists so unit tests
and dev loops can run without spinning a vector DB; once a real
adapter exists (CodeIndex pgvector in PR 5 territory) the agent
defaults to it via env.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from meta_agent.core.ports.tools import DocHit, DocSearchTool, ToolContext, ToolValidationError

_MAX_LIMIT = 20
_SNIPPET_RADIUS = 120
_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


@dataclass(frozen=True)
class DocEntry:
    """One document available to :class:`InMemoryDocSearchTool`.

    ``source_uri`` is opaque to the LLM but stable, so callers can
    re-fetch the full document through ``WebFetchTool`` or another
    adapter-specific resolver.
    """

    source_uri: str
    title: str
    body: str


class InMemoryDocSearchTool(DocSearchTool):
    """Single-process keyword-scored :class:`DocSearchTool`."""

    def __init__(self, entries: tuple[DocEntry, ...]) -> None:
        for entry in entries:
            if not entry.source_uri:
                raise ValueError("DocEntry.source_uri must be non-empty")
        self._entries = entries
        # Pre-tokenize once at construction so search-time work stays
        # O(query_tokens * #docs) rather than retokenizing every call.
        self._tokenised: list[frozenset[str]] = [
            frozenset(_tokenise((entry.title + " " + entry.body).lower())) for entry in entries
        ]

    async def search(
        self,
        ctx: ToolContext,
        *,
        query: str,
        limit: int = 5,
    ) -> tuple[DocHit, ...]:
        if not query or not query.strip():
            raise ToolValidationError("doc_search: query must be a non-empty str")
        if limit <= 0:
            raise ToolValidationError("doc_search: limit must be a positive int")
        bounded_limit = min(limit, _MAX_LIMIT)
        query_tokens = frozenset(_tokenise(query.lower()))
        if not query_tokens:
            return ()
        scored: list[tuple[float, int, DocEntry]] = []
        for index, (entry, tokens) in enumerate(zip(self._entries, self._tokenised, strict=True)):
            hits = sum(1 for token in query_tokens if token in tokens)
            if hits == 0:
                continue
            score = hits / len(query_tokens)
            scored.append((score, index, entry))
        # Highest score first; insertion order breaks ties (lower index wins).
        scored.sort(key=lambda triple: (-triple[0], triple[1]))
        return tuple(
            DocHit(
                source_uri=entry.source_uri,
                title=entry.title,
                snippet=_snippet_around(entry.body, query_tokens),
                score=score,
            )
            for score, _idx, entry in scored[:bounded_limit]
        )


def _tokenise(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _snippet_around(body: str, query_tokens: frozenset[str]) -> str:
    """Return a short window of ``body`` around the earliest matching token."""

    if not body:
        return ""
    lower = body.lower()
    earliest: int | None = None
    for token in query_tokens:
        if not token:
            continue
        idx = lower.find(token)
        if idx >= 0 and (earliest is None or idx < earliest):
            earliest = idx
    if earliest is None:
        # No token matched in the body (the match was only in the
        # title). Fall back to the first chunk of the body.
        return body[: 2 * _SNIPPET_RADIUS].rstrip()
    start = max(earliest - _SNIPPET_RADIUS, 0)
    end = min(earliest + _SNIPPET_RADIUS, len(body))
    snippet = body[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(body):
        snippet = snippet + "…"
    return snippet
