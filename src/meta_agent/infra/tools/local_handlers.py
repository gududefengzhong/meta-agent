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
)

TOOL_FS_READ = "fs_read"
TOOL_FS_LIST_DIR = "fs_list_dir"
TOOL_FS_GREP = "fs_grep"
TOOL_EDIT_WRITE = "edit_write"
TOOL_EDIT_PATCH_APPLY = "edit_patch_apply"
TOOL_SHELL_RUN = "shell_run"
TOOL_TEST_RUN = "test_run"


def _arg_str(
    args: dict[str, Any], key: str, *, required: bool = True, default: str = ""
) -> str:
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


def _arg_str_tuple(
    args: dict[str, Any], key: str, *, default: tuple[str, ...]
) -> tuple[str, ...]:
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
        entries = await fs.list_dir(
            ctx, path=path, recursive=recursive, max_entries=max_entries
        )
        return ToolResult(call_id=call.id, name=call.name, content="\n".join(entries))

    return handler


def _fs_grep_handler(fs: FileSystemTool) -> ToolHandler:
    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        pattern = _arg_str(call.arguments, "pattern")
        globs = _arg_str_tuple(call.arguments, "path_globs", default=("**/*",))
        max_matches = _arg_int(call.arguments, "max_matches", default=200)
        hits = await fs.grep(
            ctx, pattern=pattern, path_globs=globs, max_matches=max_matches
        )
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
) -> None:
    """Register the local FS/Edit/Shell tools against ``registry``.

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
