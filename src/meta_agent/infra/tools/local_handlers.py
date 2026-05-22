"""Wire local-workspace FS/Edit/Shell/Test implementations into the tool registry.

The :class:`ToolRegistry` keys handlers by tool name and treats each
handler as opaque. This module supplies the concrete
``ToolCall(name, arguments) → typed FileSystemTool / EditTool / ShellTool / TestTool method``
adapters together with the JSON-schema specs the LLM sees.

Layout (kept dull on purpose):

* Name constants (``TOOL_*``) are public so graph code can build
  ``ToolCall`` instances against the same identifiers.
* Argument helpers normalise ``dict[str, Any]`` into the typed kwargs
  the FS / Edit / Shell / Test ports expect; type mismatches raise
  :class:`ToolValidationError` so the executor renders them as an
  ``is_error=True`` observation instead of crashing the worker.
* Specs are kept as plain JSON-schema fragments; pydantic-level
  validation lives on the typed tool methods themselves, so the schema
  here is informational (it ships to the LLM) rather than enforcing.
"""

from __future__ import annotations

from typing import Any

from meta_agent.core.capabilities.registry import ToolHandler, ToolRegistry
from meta_agent.core.ports.tools import (
    CodeRetrievalTool,
    DocSearchTool,
    EditTool,
    FileSystemTool,
    ShellTool,
    TestTool,
    ToolCall,
    ToolCategory,
    ToolContext,
    ToolResult,
    ToolSpec,
    ToolValidationError,
    WebFetchTool,
)

TOOL_FS_READ = "fs_read"
TOOL_FS_LIST_DIR = "fs_list_dir"
TOOL_FS_GREP = "fs_grep"
TOOL_EDIT_WRITE = "edit_write"
TOOL_EDIT_PATCH_APPLY = "edit_patch_apply"
TOOL_SHELL_RUN = "shell_run"
TOOL_TEST_RUN = "test_run"
TOOL_WEB_FETCH = "web_fetch"
TOOL_DOC_SEARCH = "doc_search"
TOOL_CODE_SEARCH = "code_search"
TOOL_GET_DEFINITION = "get_definition"
TOOL_GET_REFERENCES = "get_references"
TOOL_OUTLINE = "outline"


def _arg_str(args: dict[str, Any], key: str, *, required: bool = True, default: str = "") -> str:
    if key not in args:
        if required:
            raise ToolValidationError(f"missing required argument {key!r}")
        return default
    value = args[key]
    if not isinstance(value, str):
        raise ToolValidationError(f"argument {key!r} must be a string")
    return value


def _arg_int(args: dict[str, Any], key: str, *, default: int) -> int:
    if key not in args:
        return default
    value = args[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolValidationError(f"argument {key!r} must be an integer")
    return value


def _arg_int_or_none(args: dict[str, Any], key: str) -> int | None:
    if key not in args or args[key] is None:
        return None
    value = args[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolValidationError(f"argument {key!r} must be an integer or null")
    return value


def _arg_bool(args: dict[str, Any], key: str, *, default: bool) -> bool:
    if key not in args:
        return default
    value = args[key]
    if not isinstance(value, bool):
        raise ToolValidationError(f"argument {key!r} must be a boolean")
    return value


def _arg_str_tuple(args: dict[str, Any], key: str, *, default: tuple[str, ...]) -> tuple[str, ...]:
    if key not in args:
        return default
    value = args[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ToolValidationError(f"argument {key!r} must be an array of strings")
    return tuple(value)


def _arg_argv(args: dict[str, Any], key: str) -> tuple[str, ...]:
    value = args.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ToolValidationError(f"argument {key!r} must be a non-empty array of strings")
    return tuple(value)


_FS_READ_SPEC = ToolSpec(
    name=TOOL_FS_READ,
    description="Read a UTF-8 slice of a file inside the workspace.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative path."},
            "offset": {"type": "integer", "minimum": 0},
            "max_bytes": {"type": ["integer", "null"]},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    category=ToolCategory.FILESYSTEM,
)

_FS_LIST_DIR_SPEC = ToolSpec(
    name=TOOL_FS_LIST_DIR,
    description="List entries under a workspace directory ('' for root).",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "recursive": {"type": "boolean"},
            "max_entries": {"type": "integer", "minimum": 1},
        },
        "required": [],
        "additionalProperties": False,
    },
    category=ToolCategory.FILESYSTEM,
)

_FS_GREP_SPEC = ToolSpec(
    name=TOOL_FS_GREP,
    description="Search a regex across files matching the given path globs.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path_globs": {"type": "array", "items": {"type": "string"}},
            "max_matches": {"type": "integer", "minimum": 1},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
    category=ToolCategory.FILESYSTEM,
)

_EDIT_WRITE_SPEC = ToolSpec(
    name=TOOL_EDIT_WRITE,
    description="Overwrite a workspace file with UTF-8 content.",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
    category=ToolCategory.EDIT,
)

_EDIT_PATCH_APPLY_SPEC = ToolSpec(
    name=TOOL_EDIT_PATCH_APPLY,
    description="Apply a unified diff against the workspace root via 'git apply'.",
    parameters={
        "type": "object",
        "properties": {
            "unified_diff": {"type": "string"},
        },
        "required": ["unified_diff"],
        "additionalProperties": False,
    },
    category=ToolCategory.EDIT,
)

_SHELL_RUN_SPEC = ToolSpec(
    name=TOOL_SHELL_RUN,
    description="Run an allow-listed command inside the workspace without invoking a shell.",
    parameters={
        "type": "object",
        "properties": {
            "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "timeout_seconds": {"type": ["integer", "null"], "minimum": 1},
        },
        "required": ["argv"],
        "additionalProperties": False,
    },
    category=ToolCategory.SHELL,
)

_TEST_RUN_SPEC = ToolSpec(
    name=TOOL_TEST_RUN,
    description="Run an allow-listed verification suite inside the workspace.",
    parameters={
        "type": "object",
        "properties": {
            "suite": {"type": "string"},
            "targets": {"type": "array", "items": {"type": "string"}},
            "timeout_seconds": {"type": ["integer", "null"], "minimum": 1},
        },
        "required": ["suite"],
        "additionalProperties": False,
    },
    category=ToolCategory.TEST,
)

_WEB_FETCH_SPEC = ToolSpec(
    name=TOOL_WEB_FETCH,
    description=(
        "Fetch a single HTTP/HTTPS URL by GET. The host must appear in the "
        "operator-configured allow-list; binary content is refused. The "
        "response body is bounded by the agent's output_byte_cap and may be "
        "truncated."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "timeout_seconds": {"type": ["number", "null"], "exclusiveMinimum": 0},
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    category=ToolCategory.WEB,
)

_DOC_SEARCH_SPEC = ToolSpec(
    name=TOOL_DOC_SEARCH,
    description=(
        "Search a configured knowledge base for documents matching a "
        "natural-language query. Returns ranked source_uri / title / "
        "snippet entries; pair with web_fetch (or an adapter-specific "
        "resolver) to retrieve full documents."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    category=ToolCategory.WEB,
)

_CODE_SEARCH_SPEC = ToolSpec(
    name=TOOL_CODE_SEARCH,
    description=(
        "Search the workspace for code matching a regex query. Returns "
        "ranked hits enriched with the enclosing symbol (function / "
        "class / method) when the file's language is supported. No "
        "persistent index — every call reads the live worktree."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "path_globs": {"type": "array", "items": {"type": "string"}},
            "language": {"type": ["string", "null"]},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
    category=ToolCategory.CODE_INDEX,
)

_GET_DEFINITION_SPEC = ToolSpec(
    name=TOOL_GET_DEFINITION,
    description=(
        "Locate every definition site of a symbol (function / class / "
        "method) inside the workspace. Symbol must be a plain "
        "identifier. Returns file paths + line ranges."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "language": {"type": ["string", "null"]},
            "path_globs": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["symbol"],
        "additionalProperties": False,
    },
    category=ToolCategory.CODE_INDEX,
)

_GET_REFERENCES_SPEC = ToolSpec(
    name=TOOL_GET_REFERENCES,
    description=(
        "Return every file:line where a symbol is referenced. This is "
        "an identifier-level grep with word boundaries; it does not "
        "perform full type-aware resolution."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string"},
            "language": {"type": ["string", "null"]},
            "path_globs": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "minimum": 1},
        },
        "required": ["symbol"],
        "additionalProperties": False,
    },
    category=ToolCategory.CODE_INDEX,
)

_OUTLINE_SPEC = ToolSpec(
    name=TOOL_OUTLINE,
    description=(
        "Return the top-level / nested symbol outline of a file "
        "(classes, functions, methods, interfaces). Pair with "
        "fs_read / get_definition once a relevant symbol is "
        "identified."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
    category=ToolCategory.CODE_INDEX,
)


def _fs_read_handler(fs: FileSystemTool) -> ToolHandler:
    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        path = _arg_str(call.arguments, "path")
        offset = _arg_int(call.arguments, "offset", default=0)
        max_bytes = _arg_int_or_none(call.arguments, "max_bytes")
        content = await fs.read(ctx, path=path, offset=offset, max_bytes=max_bytes)
        return ToolResult(call_id=call.id, name=call.name, content=content)

    return handler


def _fs_list_dir_handler(fs: FileSystemTool) -> ToolHandler:
    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        path = _arg_str(call.arguments, "path", required=False, default="")
        recursive = _arg_bool(call.arguments, "recursive", default=False)
        max_entries = _arg_int(call.arguments, "max_entries", default=1000)
        entries = await fs.list_dir(ctx, path=path, recursive=recursive, max_entries=max_entries)
        return ToolResult(call_id=call.id, name=call.name, content="\n".join(entries))

    return handler


def _fs_grep_handler(fs: FileSystemTool) -> ToolHandler:
    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        pattern = _arg_str(call.arguments, "pattern")
        globs = _arg_str_tuple(call.arguments, "path_globs", default=("**/*",))
        max_matches = _arg_int(call.arguments, "max_matches", default=200)
        hits = await fs.grep(ctx, pattern=pattern, path_globs=globs, max_matches=max_matches)
        body = "\n".join(f"{hit.path}:{hit.line_no}:{hit.line}" for hit in hits)
        return ToolResult(call_id=call.id, name=call.name, content=body)

    return handler


def _edit_write_handler(edit: EditTool) -> ToolHandler:
    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        path = _arg_str(call.arguments, "path")
        content = _arg_str(call.arguments, "content")
        outcome = await edit.write(ctx, path=path, content=content)
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content=f"wrote {outcome.bytes_written} bytes to {path}",
            metadata={"bytes_written": str(outcome.bytes_written)},
        )

    return handler


def _edit_patch_apply_handler(edit: EditTool) -> ToolHandler:
    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        diff = _arg_str(call.arguments, "unified_diff")
        outcome = await edit.patch_apply(ctx, unified_diff=diff)
        files = ", ".join(outcome.files_changed) or "(none)"
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content=f"applied diff; files: {files}",
            metadata={"files_changed": ",".join(outcome.files_changed)},
        )

    return handler


def _shell_run_handler(shell: ShellTool) -> ToolHandler:
    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        argv = _arg_argv(call.arguments, "argv")
        timeout_seconds = _arg_int_or_none(call.arguments, "timeout_seconds")
        outcome = await shell.run(ctx, argv=argv, timeout_seconds=timeout_seconds)
        stdout = outcome.stdout or "<empty>"
        stderr = outcome.stderr or "<empty>"
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content=f"exit_code={outcome.exit_code}\nstdout:\n{stdout}\nstderr:\n{stderr}",
            is_error=outcome.exit_code != 0,
            metadata={"exit_code": str(outcome.exit_code)},
        )

    return handler


def _arg_float_or_none(args: dict[str, Any], key: str) -> float | None:
    if key not in args or args[key] is None:
        return None
    value = args[key]
    if isinstance(value, bool):
        raise ToolValidationError(f"argument {key!r} must be a number or null")
    if isinstance(value, int | float):
        return float(value)
    raise ToolValidationError(f"argument {key!r} must be a number or null")


def _web_fetch_handler(web: WebFetchTool) -> ToolHandler:
    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        url = _arg_str(call.arguments, "url")
        timeout_seconds = _arg_float_or_none(call.arguments, "timeout_seconds")
        outcome = await web.fetch(ctx, url=url, timeout_seconds=timeout_seconds)
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content=(
                f"final_url={outcome.final_url}\n"
                f"status={outcome.status}\n"
                f"content_type={outcome.content_type}\n"
                f"bytes_received={outcome.bytes_received}\n"
                f"---\n{outcome.content}"
            ),
            truncated=outcome.truncated,
            is_error=not (200 <= outcome.status < 300),
            metadata={
                "status": str(outcome.status),
                "content_type": outcome.content_type,
                "bytes_received": str(outcome.bytes_received),
            },
        )

    return handler


def _code_search_handler(code: CodeRetrievalTool) -> ToolHandler:
    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        query = _arg_str(call.arguments, "query")
        path_globs = _arg_str_tuple(call.arguments, "path_globs", default=("**/*",))
        language = _arg_str(call.arguments, "language", required=False, default="") or None
        limit = _arg_int(call.arguments, "limit", default=20)
        hits = await code.search(
            ctx,
            query=query,
            path_globs=path_globs,
            language=language,
            limit=limit,
        )
        if not hits:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content=f"no matches for query {query!r}",
                metadata={"hits": "0"},
            )
        lines = [
            (
                f"[{idx + 1}] {hit.path}:{hit.line_no}"
                + (
                    f" ({hit.symbol_kind.value} {hit.symbol})"
                    if hit.symbol and hit.symbol_kind is not None
                    else ""
                )
                + f"\n    {hit.snippet}"
            )
            for idx, hit in enumerate(hits)
        ]
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content="\n".join(lines),
            metadata={"hits": str(len(hits))},
        )

    return handler


def _get_definition_handler(code: CodeRetrievalTool) -> ToolHandler:
    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        symbol = _arg_str(call.arguments, "symbol")
        path_globs = _arg_str_tuple(call.arguments, "path_globs", default=("**/*",))
        language = _arg_str(call.arguments, "language", required=False, default="") or None
        locations = await code.get_definition(
            ctx, symbol=symbol, language=language, path_globs=path_globs
        )
        if not locations:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content=f"no definition found for {symbol!r}",
                metadata={"hits": "0"},
            )
        lines = [
            (
                f"[{idx + 1}] {loc.path}:{loc.line_no}"
                + (f"-{loc.end_line_no}" if loc.end_line_no is not None else "")
                + f" ({loc.symbol_kind.value})\n    {loc.snippet}"
            )
            for idx, loc in enumerate(locations)
        ]
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content="\n".join(lines),
            metadata={"hits": str(len(locations))},
        )

    return handler


def _get_references_handler(code: CodeRetrievalTool) -> ToolHandler:
    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        symbol = _arg_str(call.arguments, "symbol")
        path_globs = _arg_str_tuple(call.arguments, "path_globs", default=("**/*",))
        language = _arg_str(call.arguments, "language", required=False, default="") or None
        limit = _arg_int(call.arguments, "limit", default=200)
        locations = await code.get_references(
            ctx,
            symbol=symbol,
            language=language,
            path_globs=path_globs,
            limit=limit,
        )
        if not locations:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content=f"no references found for {symbol!r}",
                metadata={"hits": "0"},
            )
        body = "\n".join(f"{loc.path}:{loc.line_no}: {loc.snippet}" for loc in locations)
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content=body,
            metadata={"hits": str(len(locations))},
        )

    return handler


def _outline_handler(code: CodeRetrievalTool) -> ToolHandler:
    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        path = _arg_str(call.arguments, "path")
        entries = await code.outline(ctx, path=path)
        if not entries:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content=f"no symbols extracted from {path!r}",
                metadata={"entries": "0"},
            )
        lines = [
            (
                "  " * entry.depth
                + f"{entry.line_no}"
                + (f"-{entry.end_line_no}" if entry.end_line_no is not None else "")
                + f": {entry.symbol_kind.value} {entry.symbol}"
            )
            for entry in entries
        ]
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content="\n".join(lines),
            metadata={"entries": str(len(entries))},
        )

    return handler


def _doc_search_handler(search: DocSearchTool) -> ToolHandler:
    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        query = _arg_str(call.arguments, "query")
        limit = _arg_int(call.arguments, "limit", default=5)
        hits = await search.search(ctx, query=query, limit=limit)
        if not hits:
            return ToolResult(
                call_id=call.id,
                name=call.name,
                content=f"no documents matched query {query!r}",
                metadata={"hits": "0"},
            )
        lines = [
            f"[{idx + 1}] score={hit.score:.3f} uri={hit.source_uri}\n"
            f"title: {hit.title}\nsnippet: {hit.snippet}"
            for idx, hit in enumerate(hits)
        ]
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content="\n\n".join(lines),
            metadata={"hits": str(len(hits))},
        )

    return handler


def _test_run_handler(test: TestTool) -> ToolHandler:
    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        suite = _arg_str(call.arguments, "suite")
        targets = _arg_str_tuple(call.arguments, "targets", default=())
        timeout_seconds = _arg_int_or_none(call.arguments, "timeout_seconds")
        outcome = await test.run(
            ctx,
            suite=suite,
            targets=targets,
            timeout_seconds=timeout_seconds,
        )
        stdout = outcome.stdout or "<empty>"
        stderr = outcome.stderr or "<empty>"
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content=(
                f"suite={outcome.suite}\n"
                f"exit_code={outcome.exit_code}\n"
                f"stdout:\n{stdout}\n"
                f"stderr:\n{stderr}"
            ),
            is_error=outcome.exit_code != 0,
            metadata={
                "suite": outcome.suite,
                "exit_code": str(outcome.exit_code),
            },
        )

    return handler


def register_local_workspace_tools(
    registry: ToolRegistry,
    *,
    fs: FileSystemTool,
    edit: EditTool,
    shell: ShellTool,
    test: TestTool | None = None,
    web_fetch: WebFetchTool | None = None,
    doc_search: DocSearchTool | None = None,
    code_retrieval: CodeRetrievalTool | None = None,
) -> None:
    """Register the local FS/Edit/Shell tools against ``registry``.

    ``web_fetch`` and ``doc_search`` are optional — bootstraps that do
    not configure the WEB surface (no domain allow-list, no doc
    corpus) simply skip those registrations, and the agent loop never
    sees the tools.

    Idempotency is the registry's responsibility (duplicate names raise
    :class:`ToolValidationError`); call this exactly once at boot.
    """

    registry.register(_FS_READ_SPEC, _fs_read_handler(fs))
    registry.register(_FS_LIST_DIR_SPEC, _fs_list_dir_handler(fs))
    registry.register(_FS_GREP_SPEC, _fs_grep_handler(fs))
    registry.register(_EDIT_WRITE_SPEC, _edit_write_handler(edit))
    registry.register(_EDIT_PATCH_APPLY_SPEC, _edit_patch_apply_handler(edit))
    registry.register(_SHELL_RUN_SPEC, _shell_run_handler(shell))
    if test is not None:
        registry.register(_TEST_RUN_SPEC, _test_run_handler(test))
    if web_fetch is not None:
        registry.register(_WEB_FETCH_SPEC, _web_fetch_handler(web_fetch))
    if doc_search is not None:
        registry.register(_DOC_SEARCH_SPEC, _doc_search_handler(doc_search))
    if code_retrieval is not None:
        registry.register(_CODE_SEARCH_SPEC, _code_search_handler(code_retrieval))
        registry.register(_GET_DEFINITION_SPEC, _get_definition_handler(code_retrieval))
        registry.register(_GET_REFERENCES_SPEC, _get_references_handler(code_retrieval))
        registry.register(_OUTLINE_SPEC, _outline_handler(code_retrieval))
