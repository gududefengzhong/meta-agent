"""Tree-sitter backed :class:`CodeRetrievalTool` implementation.

Stateless on purpose: every call walks the live workspace, parses on
demand, and returns. No persistent index, no cache invalidation, no
version table — refactor-heavy iterations stay correct by
construction (the next call reads the latest on-disk content).

Performance shape:

* ``search`` does a substring/regex pass first to identify candidate
  files, then parses only those files. On a 10k-file repo with a
  query that matches 30 files, total work is ~30 file reads + 30
  parses (~30ms parse cost on Python; tree-sitter is fast).
* ``get_definition`` / ``get_references`` walk the workspace once,
  filtering by extension first, then parse + match.
* ``outline`` parses exactly one file.

Boundaries:

* Paths outside ``ctx.workspace_path`` are refused as
  :class:`ToolValidationError`.
* Files larger than :data:`_MAX_FILE_BYTES` are skipped silently
  during traversal (avoids accidentally parsing huge generated
  bundles); ``outline`` of such a file raises
  :class:`ToolValidationError` to surface the limit.
* Hidden directories / version-control / virtualenv noise is filtered
  via :data:`_SKIPPED_DIRS`. The set is intentionally conservative;
  callers narrow further with ``path_globs``.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterator
from pathlib import Path

from tree_sitter import Node, QueryCursor

from meta_agent.core.ports.tools import (
    CodeHit,
    CodeLocation,
    CodeRetrievalTool,
    OutlineEntry,
    SymbolKind,
    ToolContext,
    ToolValidationError,
)
from meta_agent.infra.tools.code_index.languages import (
    SUPPORTED_LANGUAGES,
    capture_to_kind,
    language_extensions,
    language_for_path,
    make_parser,
    symbol_query,
)

_MAX_FILE_BYTES = 1_000_000
"""Files larger than this are skipped during walks (1 MB)."""

_SKIPPED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "dist",
        "build",
    }
)


class TreeSitterCodeRetrievalTool(CodeRetrievalTool):
    """On-demand tree-sitter retrieval bound to ``ctx.workspace_path``."""

    async def search(
        self,
        ctx: ToolContext,
        *,
        query: str,
        path_globs: tuple[str, ...] = ("**/*",),
        language: str | None = None,
        limit: int = 20,
    ) -> tuple[CodeHit, ...]:
        if not query or not query.strip():
            raise ToolValidationError("code_search: query must be non-empty")
        if limit <= 0:
            raise ToolValidationError("code_search: limit must be a positive int")
        workspace = _require_workspace(ctx)
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error as exc:
            raise ToolValidationError(f"code_search: invalid regex {query!r}: {exc}") from exc

        hits: list[CodeHit] = []
        for path in _iter_workspace(workspace, path_globs, language):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            file_hits = list(_search_in_file(workspace, path, text, pattern))
            hits.extend(file_hits)
            if len(hits) >= limit:
                break
        hits.sort(key=lambda hit: (-hit.score, hit.path, hit.line_no))
        return tuple(hits[:limit])

    async def get_definition(
        self,
        ctx: ToolContext,
        *,
        symbol: str,
        language: str | None = None,
        path_globs: tuple[str, ...] = ("**/*",),
    ) -> tuple[CodeLocation, ...]:
        _check_symbol(symbol)
        workspace = _require_workspace(ctx)
        identifier_re = re.compile(rf"\b{re.escape(symbol)}\b")

        results: list[CodeLocation] = []
        for path in _iter_workspace(workspace, path_globs, language):
            try:
                source = path.read_bytes()
            except OSError:
                continue
            if identifier_re.search(source.decode("utf-8", errors="replace")) is None:
                continue
            file_language = language_for_path(str(path))
            if file_language is None:
                continue
            results.extend(_extract_definitions(workspace, path, source, file_language, symbol))
        results.sort(key=lambda loc: (loc.path, loc.line_no))
        return tuple(results)

    async def get_references(
        self,
        ctx: ToolContext,
        *,
        symbol: str,
        language: str | None = None,
        path_globs: tuple[str, ...] = ("**/*",),
        limit: int = 200,
    ) -> tuple[CodeLocation, ...]:
        _check_symbol(symbol)
        if limit <= 0:
            raise ToolValidationError("get_references: limit must be a positive int")
        workspace = _require_workspace(ctx)
        identifier_re = re.compile(rf"\b{re.escape(symbol)}\b")

        results: list[CodeLocation] = []
        for path in _iter_workspace(workspace, path_globs, language):
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if identifier_re.search(line) is None:
                    continue
                results.append(
                    CodeLocation(
                        path=str(path.relative_to(workspace)),
                        line_no=line_no,
                        symbol=symbol,
                        symbol_kind=SymbolKind.OTHER,
                        snippet=line.strip(),
                    )
                )
                if len(results) >= limit:
                    return tuple(results)
        return tuple(results)

    async def outline(
        self,
        ctx: ToolContext,
        *,
        path: str,
    ) -> tuple[OutlineEntry, ...]:
        workspace = _require_workspace(ctx)
        target = _resolve_path(workspace, path)
        if not target.is_file():
            raise ToolValidationError(f"outline: {path!r} is not a file")
        if target.stat().st_size > _MAX_FILE_BYTES:
            raise ToolValidationError(
                f"outline: {path!r} exceeds max file size {_MAX_FILE_BYTES} bytes"
            )
        language = language_for_path(str(target))
        if language is None:
            raise ToolValidationError(
                f"outline: {path!r} has no supported language; expected one of "
                f"{SUPPORTED_LANGUAGES}"
            )
        source = target.read_bytes()
        return tuple(_extract_outline(source, language))


# ---------------------------------------------------------------------------
# Helpers below: pure functions kept module-level so they stay easy to test
# in isolation.
# ---------------------------------------------------------------------------


def _require_workspace(ctx: ToolContext) -> Path:
    if ctx.workspace_path is None:
        raise ToolValidationError(
            "code_index: ctx.workspace_path is required; the agent must be running "
            "against a provisioned workspace"
        )
    return ctx.workspace_path


def _check_symbol(symbol: str) -> None:
    if not symbol or not symbol.strip():
        raise ToolValidationError("code_index: symbol must be non-empty")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", symbol):
        raise ToolValidationError(f"code_index: symbol {symbol!r} is not a plain identifier")


def _resolve_path(workspace: Path, path: str) -> Path:
    candidate = (workspace / path).resolve()
    workspace_resolved = workspace.resolve()
    if workspace_resolved not in candidate.parents and candidate != workspace_resolved:
        raise ToolValidationError(f"code_index: path {path!r} escapes the workspace")
    return candidate


def _iter_workspace(
    workspace: Path,
    path_globs: tuple[str, ...],
    language: str | None,
) -> Iterator[Path]:
    extensions = set(language_extensions(language))
    workspace_resolved = workspace.resolve()
    # Empty path_globs / the sentinel ("**/*",) means "any file"; skip
    # fnmatch entirely so callers do not need to know glob-engine
    # quirks (fnmatch.fnmatch does not treat ``**`` recursively).
    match_any = not path_globs or path_globs == ("**/*",)
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        # Skip noisy directories.
        if any(part in _SKIPPED_DIRS for part in path.relative_to(workspace).parts[:-1]):
            continue
        if path.suffix.lower() not in extensions:
            continue
        if path.stat().st_size > _MAX_FILE_BYTES:
            continue
        rel = str(path.relative_to(workspace))
        if not match_any and not _matches_any_glob(rel, path_globs):
            continue
        # Defensive resolution to prevent symlink escapes.
        if workspace_resolved not in path.resolve().parents:
            continue
        yield path


def _matches_any_glob(rel_path: str, globs: tuple[str, ...]) -> bool:
    """Return ``True`` when ``rel_path`` matches any caller-supplied glob.

    ``**`` is normalised to ``*`` recursively because tree-sitter
    retrieval is already walking from the workspace root — the
    distinction between "match a single path segment" and "match many
    segments" has no observable effect once every visited path is a
    file under the workspace.
    """

    return any(fnmatch.fnmatch(rel_path, _normalise_glob(glob)) for glob in globs)


def _normalise_glob(glob: str) -> str:
    return glob.replace("**/", "").replace("**", "*")


def _search_in_file(
    workspace: Path, path: Path, text: str, pattern: re.Pattern[str]
) -> Iterator[CodeHit]:
    rel = str(path.relative_to(workspace))
    file_language = language_for_path(str(path))
    # Pre-parse only when this file is language-recognised; symbol
    # enrichment is best-effort and cheap to skip on unknown formats.
    symbol_index = (
        _build_symbol_index(path.read_bytes(), file_language) if file_language is not None else ()
    )
    seen_lines: set[int] = set()
    for line_no, line in enumerate(text.splitlines(), start=1):
        if line_no in seen_lines:
            continue
        match = pattern.search(line)
        if match is None:
            continue
        seen_lines.add(line_no)
        enclosing = _enclosing_symbol(symbol_index, line_no)
        symbol_name = enclosing[0] if enclosing is not None else None
        symbol_kind = enclosing[1] if enclosing is not None else None
        # Score: more matches → higher rank. Cap so per-file
        # tie-breaks don't drown out cross-file diversity.
        score = min(len(pattern.findall(line)) + 1, 5) / 5.0
        yield CodeHit(
            path=rel,
            line_no=line_no,
            symbol=symbol_name,
            symbol_kind=symbol_kind,
            snippet=line.strip(),
            score=score,
        )


_SymbolIndexEntry = tuple[int, int, str, SymbolKind]
"""``(start_line, end_line, name, kind)`` — used by the enclosing-symbol lookup."""


def _build_symbol_index(source: bytes, language: str) -> tuple[_SymbolIndexEntry, ...]:
    parser = make_parser(language)
    tree = parser.parse(source)
    cursor = QueryCursor(symbol_query(language))
    entries: list[_SymbolIndexEntry] = []
    matches = cursor.matches(tree.root_node)
    for _pattern_index, captures in matches:
        kind_capture: tuple[str, Node] | None = None
        name_node: Node | None = None
        for capture_name, nodes in captures.items():
            if capture_name == "name":
                name_node = nodes[0]
                continue
            # Any other capture name carries the kind hint.
            kind_capture = (capture_name, nodes[0])
        if name_node is None or kind_capture is None:
            continue
        kind = capture_to_kind(kind_capture[0])
        outer_node = kind_capture[1]
        entries.append(
            (
                outer_node.start_point[0] + 1,
                outer_node.end_point[0] + 1,
                name_node.text.decode("utf-8", errors="replace") if name_node.text else "",
                kind,
            )
        )
    # Sort by start line, then by widest range first so a later
    # enclosing-symbol lookup picks the innermost wrapper when scopes
    # are nested.
    entries.sort(key=lambda entry: (entry[0], -(entry[1] - entry[0])))
    return tuple(entries)


def _enclosing_symbol(
    index: tuple[_SymbolIndexEntry, ...], line_no: int
) -> tuple[str, SymbolKind] | None:
    best: tuple[str, SymbolKind] | None = None
    best_span: int | None = None
    for start, end, name, kind in index:
        if start <= line_no <= end:
            span = end - start
            if best_span is None or span < best_span:
                best = (name, kind)
                best_span = span
    return best


def _extract_definitions(
    workspace: Path,
    path: Path,
    source: bytes,
    language: str,
    symbol: str,
) -> Iterator[CodeLocation]:
    index = _build_symbol_index(source, language)
    rel = str(path.relative_to(workspace))
    text = source.decode("utf-8", errors="replace")
    lines = text.splitlines()
    for start, end, name, kind in index:
        if name != symbol:
            continue
        snippet = lines[start - 1].strip() if 1 <= start <= len(lines) else ""
        yield CodeLocation(
            path=rel,
            line_no=start,
            end_line_no=end,
            symbol=name,
            symbol_kind=kind,
            snippet=snippet,
        )


def _extract_outline(source: bytes, language: str) -> Iterator[OutlineEntry]:
    index = _build_symbol_index(source, language)
    # Compute depth by counting how many other entries strictly
    # contain this one. Sort + scan keeps the cost at O(n log n) on
    # the typically small symbol count per file.
    sorted_index = sorted(index, key=lambda entry: (entry[0], -(entry[1] - entry[0])))
    for start, end, name, kind in sorted_index:
        depth = sum(
            1
            for outer_start, outer_end, _, _ in sorted_index
            if (outer_start < start <= outer_end and outer_end >= end)
            or (outer_start <= start and outer_end > end and outer_start != start)
        )
        yield OutlineEntry(
            line_no=start,
            end_line_no=end,
            symbol=name,
            symbol_kind=kind,
            depth=depth,
        )
