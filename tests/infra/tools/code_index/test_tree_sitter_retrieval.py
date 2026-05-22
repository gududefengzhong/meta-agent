"""Unit tests for :class:`TreeSitterCodeRetrievalTool`."""

from __future__ import annotations

from pathlib import Path

import pytest

from meta_agent.core.ports.tools import (
    SymbolKind,
    ToolContext,
    ToolValidationError,
)
from meta_agent.infra.tools.code_index import TreeSitterCodeRetrievalTool


def _ctx(workspace: Path) -> ToolContext:
    return ToolContext(
        tenant_id="t",
        task_id="task",
        trace_id="trace",
        workspace_path=workspace,
        output_byte_cap=64_000,
    )


_PYTHON_FIXTURE = """\
def add(a, b):
    return a + b


class Calc:
    def mul(self, a, b):
        return a * b

    def div(self, a, b):
        return a / b


def add(extra):
    # second definition; tree-sitter sees it as a top-level function too.
    return extra
"""

_TYPESCRIPT_FIXTURE = """\
export class Calculator {
    add(a: number, b: number): number {
        return a + b;
    }
}

export interface Greeter {
    greet(name: string): string;
}

export function greet(name: string): string {
    return `hi ${name}`;
}
"""


@pytest.fixture
def py_workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(_PYTHON_FIXTURE, encoding="utf-8")
    (tmp_path / "src" / "__init__.py").write_text("", encoding="utf-8")
    # Noisy directory the walker should skip.
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text(
        "def should_not_appear():\n    return 0\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def ts_workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "greeter.ts").write_text(_TYPESCRIPT_FIXTURE, encoding="utf-8")
    return tmp_path


async def test_outline_extracts_python_symbols(py_workspace: Path) -> None:
    tool = TreeSitterCodeRetrievalTool()
    entries = await tool.outline(_ctx(py_workspace), path="src/calc.py")
    by_symbol = {(e.symbol, e.symbol_kind, e.depth) for e in entries}
    assert ("add", SymbolKind.FUNCTION, 0) in by_symbol
    assert ("Calc", SymbolKind.CLASS, 0) in by_symbol
    assert ("mul", SymbolKind.METHOD, 1) in by_symbol
    assert ("div", SymbolKind.METHOD, 1) in by_symbol


async def test_outline_extracts_typescript_symbols(ts_workspace: Path) -> None:
    tool = TreeSitterCodeRetrievalTool()
    entries = await tool.outline(_ctx(ts_workspace), path="src/greeter.ts")
    kinds = {(e.symbol, e.symbol_kind) for e in entries}
    assert ("Calculator", SymbolKind.CLASS) in kinds
    assert ("Greeter", SymbolKind.INTERFACE) in kinds
    assert ("greet", SymbolKind.FUNCTION) in kinds
    assert ("add", SymbolKind.METHOD) in kinds


async def test_get_definition_returns_every_definition_site(py_workspace: Path) -> None:
    tool = TreeSitterCodeRetrievalTool()
    locations = await tool.get_definition(_ctx(py_workspace), symbol="add")
    # Two top-level ``add`` definitions in the fixture.
    assert len(locations) == 2
    assert all(loc.symbol == "add" for loc in locations)
    assert all(loc.symbol_kind == SymbolKind.FUNCTION for loc in locations)


async def test_get_definition_finds_method_inside_class(py_workspace: Path) -> None:
    tool = TreeSitterCodeRetrievalTool()
    locations = await tool.get_definition(_ctx(py_workspace), symbol="mul")
    assert len(locations) == 1
    assert locations[0].symbol == "mul"
    assert locations[0].symbol_kind == SymbolKind.METHOD


async def test_get_definition_rejects_invalid_identifier(py_workspace: Path) -> None:
    tool = TreeSitterCodeRetrievalTool()
    with pytest.raises(ToolValidationError):
        await tool.get_definition(_ctx(py_workspace), symbol="has space")
    with pytest.raises(ToolValidationError):
        await tool.get_definition(_ctx(py_workspace), symbol="")


async def test_get_references_returns_every_occurrence(py_workspace: Path) -> None:
    tool = TreeSitterCodeRetrievalTool()
    locations = await tool.get_references(_ctx(py_workspace), symbol="Calc")
    assert len(locations) == 1
    assert locations[0].path == "src/calc.py"
    assert "Calc" in locations[0].snippet


async def test_get_references_respects_limit(py_workspace: Path) -> None:
    tool = TreeSitterCodeRetrievalTool()
    locations = await tool.get_references(_ctx(py_workspace), symbol="a", limit=2)
    assert len(locations) <= 2


async def test_search_returns_hits_enriched_with_enclosing_symbol(py_workspace: Path) -> None:
    tool = TreeSitterCodeRetrievalTool()
    hits = await tool.search(_ctx(py_workspace), query=r"return a \*")
    assert len(hits) >= 1
    matching = [h for h in hits if "a *" in h.snippet]
    assert matching, "expected a hit containing 'a *'"
    enclosing = matching[0]
    assert enclosing.symbol == "mul"
    assert enclosing.symbol_kind == SymbolKind.METHOD


async def test_search_rejects_invalid_regex(py_workspace: Path) -> None:
    tool = TreeSitterCodeRetrievalTool()
    with pytest.raises(ToolValidationError):
        await tool.search(_ctx(py_workspace), query="[unclosed")


async def test_search_filters_by_path_globs(py_workspace: Path) -> None:
    tool = TreeSitterCodeRetrievalTool()
    # Restrict to a path that no file in the fixture matches.
    hits = await tool.search(_ctx(py_workspace), query="return", path_globs=("tests/*.py",))
    assert hits == ()


async def test_walker_skips_noisy_directories(py_workspace: Path) -> None:
    """``.venv`` content must never surface in any retrieval call."""

    tool = TreeSitterCodeRetrievalTool()
    refs = await tool.get_references(_ctx(py_workspace), symbol="should_not_appear")
    assert refs == ()


async def test_outline_rejects_unsupported_language(tmp_path: Path) -> None:
    (tmp_path / "README.txt").write_text("not code", encoding="utf-8")
    tool = TreeSitterCodeRetrievalTool()
    with pytest.raises(ToolValidationError, match="supported language"):
        await tool.outline(_ctx(tmp_path), path="README.txt")


async def test_outline_rejects_directory_traversal(py_workspace: Path) -> None:
    tool = TreeSitterCodeRetrievalTool()
    with pytest.raises(ToolValidationError, match="escapes"):
        await tool.outline(_ctx(py_workspace), path="../escape.py")


async def test_workspace_required(tmp_path: Path) -> None:
    ctx = ToolContext(
        tenant_id="t",
        task_id="x",
        trace_id="y",
        workspace_path=None,
        output_byte_cap=64_000,
    )
    tool = TreeSitterCodeRetrievalTool()
    with pytest.raises(ToolValidationError, match="workspace_path"):
        await tool.search(ctx, query="anything")
