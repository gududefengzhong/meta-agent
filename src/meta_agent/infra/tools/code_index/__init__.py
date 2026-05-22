"""Tree-sitter backed :class:`CodeRetrievalTool` (Phase β+ PR 5).

Stateless by construction: every public method reads the live
workspace, parses what it needs on demand, and returns. There is no
persistent index, no version table, no cache invalidation. Refactor-
heavy iterations stay correct because the next call always sees the
current on-disk state.

Sub-modules:

* :mod:`meta_agent.infra.tools.code_index.languages` — tree-sitter
  parser factories + per-language symbol queries.
* :mod:`meta_agent.infra.tools.code_index.tree_sitter_retrieval` —
  the :class:`CodeRetrievalTool` adapter itself.
"""

from meta_agent.infra.tools.code_index.tree_sitter_retrieval import (
    TreeSitterCodeRetrievalTool,
)

__all__ = ["TreeSitterCodeRetrievalTool"]
