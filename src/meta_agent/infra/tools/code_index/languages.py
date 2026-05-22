"""Tree-sitter language registry + per-language symbol queries.

One :class:`LanguageSpec` per supported language. The spec packages:

* the tree-sitter ``Language`` handle (loaded lazily so importing this
  module does not touch the disk);
* a list of file-extension globs used to map a path → language;
* a query string in tree-sitter S-expression syntax that returns
  ``(name @symbol kind @kind range @range)`` captures, where the
  adapter maps ``kind`` text to :class:`SymbolKind`.

Adding a language is a one-file change: add a new ``LanguageSpec``
entry, supply the query, register the extensions. No call sites
require modification.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable
from dataclasses import dataclass

import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Parser, Query

from meta_agent.core.ports.tools import SymbolKind

_PYTHON_SYMBOL_QUERY = """
(module
  (function_definition
    name: (identifier) @name) @function)

(module
  (decorated_definition
    definition: (function_definition
      name: (identifier) @name)) @function)

(module
  (class_definition
    name: (identifier) @name) @class)

(module
  (decorated_definition
    definition: (class_definition
      name: (identifier) @name)) @class)

(class_definition
  body: (block
    (function_definition
      name: (identifier) @name) @method))

(class_definition
  body: (block
    (decorated_definition
      definition: (function_definition
        name: (identifier) @name)) @method))
"""

_TYPESCRIPT_SYMBOL_QUERY = """
(function_declaration
  name: (identifier) @name) @function

(class_declaration
  name: (type_identifier) @name) @class

(interface_declaration
  name: (type_identifier) @name) @interface

(method_definition
  name: (property_identifier) @name) @method

(export_statement
  declaration: (function_declaration
    name: (identifier) @name)) @function

(export_statement
  declaration: (class_declaration
    name: (type_identifier) @name)) @class

(export_statement
  declaration: (interface_declaration
    name: (type_identifier) @name)) @interface
"""


# Capture name → SymbolKind. Identical across languages so query
# authoring stays uniform.
_CAPTURE_TO_KIND: dict[str, SymbolKind] = {
    "function": SymbolKind.FUNCTION,
    "method": SymbolKind.METHOD,
    "class": SymbolKind.CLASS,
    "interface": SymbolKind.INTERFACE,
    "variable": SymbolKind.VARIABLE,
    "constant": SymbolKind.CONSTANT,
}


@dataclass(frozen=True)
class LanguageSpec:
    """Configuration for one tree-sitter language."""

    name: str
    extensions: tuple[str, ...]
    _language_loader: object  # callable returning the raw tree-sitter Language pointer
    symbol_query: str


_LANGUAGES: tuple[LanguageSpec, ...] = (
    LanguageSpec(
        name="python",
        extensions=(".py",),
        _language_loader=tree_sitter_python.language,
        symbol_query=_PYTHON_SYMBOL_QUERY,
    ),
    LanguageSpec(
        name="typescript",
        extensions=(".ts", ".tsx"),
        _language_loader=tree_sitter_typescript.language_typescript,
        symbol_query=_TYPESCRIPT_SYMBOL_QUERY,
    ),
)


SUPPORTED_LANGUAGES: tuple[str, ...] = tuple(spec.name for spec in _LANGUAGES)


def language_for_path(path: str) -> str | None:
    """Resolve the language for ``path`` by extension; ``None`` if unknown."""

    lower = path.lower()
    for spec in _LANGUAGES:
        if any(lower.endswith(ext) for ext in spec.extensions):
            return spec.name
    return None


def language_extensions(language: str | None = None) -> tuple[str, ...]:
    """Return the file extensions for ``language`` (or every supported one)."""

    if language is None:
        return tuple(ext for spec in _LANGUAGES for ext in spec.extensions)
    spec = _spec_for(language)
    return spec.extensions


def _spec_for(language: str) -> LanguageSpec:
    for spec in _LANGUAGES:
        if spec.name == language:
            return spec
    raise ValueError(f"unsupported language {language!r}; expected one of {SUPPORTED_LANGUAGES}")


@functools.lru_cache(maxsize=8)
def _load_language(language: str) -> Language:
    spec = _spec_for(language)
    loader = spec._language_loader
    return Language(loader())  # type: ignore[operator]


@functools.lru_cache(maxsize=8)
def _load_query(language: str) -> Query:
    spec = _spec_for(language)
    return Query(_load_language(language), spec.symbol_query)


def make_parser(language: str) -> Parser:
    """Return a fresh :class:`tree_sitter.Parser` bound to ``language``."""

    return Parser(_load_language(language))


def symbol_query(language: str) -> Query:
    """Return the compiled symbol :class:`tree_sitter.Query` for ``language``."""

    return _load_query(language)


def capture_to_kind(capture_name: str) -> SymbolKind:
    """Map a tree-sitter capture name back to :class:`SymbolKind`."""

    return _CAPTURE_TO_KIND.get(capture_name, SymbolKind.OTHER)


def all_supported_extensions() -> Iterable[str]:
    """Iterator of every extension currently in the language registry."""

    for spec in _LANGUAGES:
        yield from spec.extensions
