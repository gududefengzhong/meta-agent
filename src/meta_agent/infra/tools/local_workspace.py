"""Local-worktree adapters for :class:`FileSystemTool` / :class:`EditTool`.

These adapters back the Phase β tool surface against a single
per-task git worktree on the worker host — the same path the
:class:`WorkspaceManager` provisions for ``builtin.bug_fix``. They
exist so the agent loop has something concrete to call before the
container-isolated sandbox lands; once the Docker workspace ships,
this module stays as the local-dev fallback.

Containment model: every path argument is resolved against
``ctx.workspace_path`` with ``Path.resolve()``; if the resolved
target falls outside the workspace root (via ``..``, an absolute
path, or a symlink), the operation raises
:class:`ToolPermissionError`. The local adapter is *not* a security
boundary — that's the sandbox's job — but it is a correctness
boundary: agent loops accidentally referencing system files fail
loudly instead of leaking host state.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import Iterable
from pathlib import Path

from meta_agent.core.ports.tools import (
    EditOutcome,
    EditTool,
    FileSystemTool,
    GrepHit,
    ShellOutcome,
    ShellTool,
    TestOutcome,
    TestTool,
    ToolContext,
    ToolExecutionError,
    ToolPermissionError,
    ToolValidationError,
)

_GIT_APPLY_TIMEOUT_SECONDS = 60.0
_DEFAULT_SHELL_TIMEOUT_SECONDS = 60
_DEFAULT_TEST_TIMEOUT_SECONDS = 120


def _require_workspace(ctx: ToolContext) -> Path:
    if ctx.workspace_path is None:
        raise ToolPermissionError("tool requires a workspace but ctx.workspace_path is None")
    return ctx.workspace_path


def _effective_cap(ctx: ToolContext, requested: int | None = None) -> int:
    cap = ctx.output_byte_cap if requested is None else min(ctx.output_byte_cap, requested)
    if cap <= 0:
        raise ToolValidationError("max_bytes must be positive")
    return cap


def _resolve_within(workspace: Path, raw_path: str) -> Path:
    """Resolve ``raw_path`` against ``workspace``, refusing escapes.

    Returns the resolved absolute path. Symlink targets that fall
    outside the workspace are rejected — this is the chokepoint for
    sandbox containment in the local adapter.
    """

    if not raw_path:
        raise ToolValidationError("path must not be empty")
    candidate = (workspace / raw_path).resolve()
    workspace_resolved = workspace.resolve()
    try:
        candidate.relative_to(workspace_resolved)
    except ValueError as exc:
        raise ToolPermissionError(f"path {raw_path!r} escapes the workspace") from exc
    return candidate


class LocalWorkspaceFileSystemTool(FileSystemTool):
    """Read-only filesystem view bound to ``ctx.workspace_path``.

    All operations run on a worker thread (``asyncio.to_thread``) so
    a slow disk does not block the event loop. Read/list/grep all
    honour the workspace containment rule defined at module level.
    """

    async def read(
        self,
        ctx: ToolContext,
        *,
        path: str,
        offset: int = 0,
        max_bytes: int | None = None,
    ) -> str:
        if offset < 0:
            raise ToolValidationError("offset must be non-negative")
        workspace = _require_workspace(ctx)
        target = _resolve_within(workspace, path)
        if not target.is_file():
            raise ToolExecutionError(f"path {path!r} is not a regular file")
        cap = _effective_cap(ctx, max_bytes)
        try:
            data = await asyncio.to_thread(_read_slice, target, offset, cap)
        except OSError as exc:
            raise ToolExecutionError(f"read failed: {exc}") from exc
        return data.decode("utf-8", errors="replace")

    async def list_dir(
        self,
        ctx: ToolContext,
        *,
        path: str,
        recursive: bool = False,
        max_entries: int = 1000,
    ) -> tuple[str, ...]:
        if max_entries <= 0:
            raise ToolValidationError("max_entries must be positive")
        workspace = _require_workspace(ctx)
        target = _resolve_within(workspace, path) if path else workspace.resolve()
        if not target.is_dir():
            raise ToolExecutionError(f"path {path!r} is not a directory")
        return await asyncio.to_thread(
            _list_entries,
            target,
            workspace.resolve(),
            recursive,
            max_entries,
            ctx.output_byte_cap,
        )

    async def grep(
        self,
        ctx: ToolContext,
        *,
        pattern: str,
        path_globs: tuple[str, ...] = ("**/*",),
        max_matches: int = 200,
    ) -> tuple[GrepHit, ...]:
        if max_matches <= 0:
            raise ToolValidationError("max_matches must be positive")
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ToolValidationError(f"invalid regex: {exc}") from exc
        workspace = _require_workspace(ctx).resolve()
        return await asyncio.to_thread(
            _grep,
            workspace,
            compiled,
            path_globs,
            max_matches,
            ctx.output_byte_cap,
        )


def _read_slice(target: Path, offset: int, max_bytes: int) -> bytes:
    with target.open("rb") as fh:
        if offset:
            fh.seek(offset)
        return fh.read(max_bytes)


def _list_entries(
    target: Path,
    workspace_root: Path,
    recursive: bool,
    max_entries: int,
    output_byte_cap: int,
) -> tuple[str, ...]:
    out: list[str] = []
    used = 0
    iterator: Iterable[Path] = target.rglob("*") if recursive else target.iterdir()
    for entry in sorted(iterator, key=lambda p: p.as_posix()):
        try:
            rel = entry.resolve().relative_to(workspace_root)
        except ValueError:
            # Symlink pointing outside the workspace; skip rather than leak.
            continue
        suffix = "/" if entry.is_dir() else ""
        rendered = f"{rel.as_posix()}{suffix}"
        rendered_bytes = len(rendered.encode("utf-8"))
        sep_bytes = 1 if out else 0
        if used + sep_bytes + rendered_bytes > output_byte_cap:
            break
        out.append(rendered)
        used += sep_bytes + rendered_bytes
        if len(out) >= max_entries:
            break
    return tuple(out)


def _grep(
    workspace: Path,
    compiled: re.Pattern[str],
    globs: tuple[str, ...],
    max_matches: int,
    output_byte_cap: int,
) -> tuple[GrepHit, ...]:
    hits: list[GrepHit] = []
    seen: set[Path] = set()
    used = 0
    for glob in globs:
        for entry in sorted(workspace.glob(glob), key=lambda p: p.as_posix()):
            if not entry.is_file() or entry in seen:
                continue
            seen.add(entry)
            try:
                rel = entry.resolve().relative_to(workspace)
            except ValueError:
                continue
            try:
                text = entry.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line_no, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line):
                    rendered = f"{rel.as_posix()}:{line_no}:{line}"
                    rendered_bytes = len(rendered.encode("utf-8"))
                    sep_bytes = 1 if hits else 0
                    if used + sep_bytes + rendered_bytes > output_byte_cap:
                        return tuple(hits)
                    hits.append(GrepHit(path=rel.as_posix(), line_no=line_no, line=line))
                    used += sep_bytes + rendered_bytes
                    if len(hits) >= max_matches:
                        return tuple(hits)
    return tuple(hits)


class LocalWorkspaceEditTool(EditTool):
    """Writable surface bound to ``ctx.workspace_path``.

    ``write`` overwrites a single file under the workspace root with
    atomic-rename semantics (write-tmp, then ``replace``);
    ``patch_apply`` shells to ``git apply`` so diff semantics mirror
    the rest of the codebase (``builtin.bug_fix`` already commits via
    the local ``git``). Both surface non-zero outcomes as
    :class:`ToolExecutionError` so the agent loop sees a typed error.
    """

    async def write(
        self,
        ctx: ToolContext,
        *,
        path: str,
        content: str,
    ) -> EditOutcome:
        workspace = _require_workspace(ctx)
        target = _resolve_within(workspace, path)
        encoded = content.encode("utf-8")
        try:
            await asyncio.to_thread(_atomic_write, target, encoded)
        except OSError as exc:
            raise ToolExecutionError(f"write failed: {exc}") from exc
        return EditOutcome(files_changed=(path,), bytes_written=len(encoded))

    async def patch_apply(
        self,
        ctx: ToolContext,
        *,
        unified_diff: str,
    ) -> EditOutcome:
        if not unified_diff.strip():
            raise ToolValidationError("unified_diff must not be empty")
        workspace = _require_workspace(ctx)
        proc = await asyncio.create_subprocess_exec(
            "git",
            "apply",
            "--whitespace=nowarn",
            "-",
            cwd=str(workspace),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr_b = await asyncio.wait_for(
                proc.communicate(unified_diff.encode("utf-8")),
                timeout=_GIT_APPLY_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise ToolExecutionError("git apply timed out") from exc
        if proc.returncode != 0:
            stderr = stderr_b.decode("utf-8", errors="replace").strip()
            raise ToolExecutionError(f"git apply failed: {stderr}")
        files_changed = _extract_diff_files(unified_diff)
        return EditOutcome(
            files_changed=files_changed,
            bytes_written=len(unified_diff.encode("utf-8")),
        )


class LocalWorkspaceShellTool(ShellTool):
    """Subprocess-backed shell tool bound to ``ctx.workspace_path``."""

    def __init__(
        self,
        *,
        allowed_commands: frozenset[str] | None = None,
        default_timeout_seconds: int = _DEFAULT_SHELL_TIMEOUT_SECONDS,
    ) -> None:
        self._allowed_commands = allowed_commands or frozenset(
            {"ruff", "python", "python3", "pytest"}
        )
        if not self._allowed_commands:
            raise ValueError("allowed_commands must not be empty")
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive")
        self._default_timeout_seconds = default_timeout_seconds

    async def run(
        self,
        ctx: ToolContext,
        *,
        argv: tuple[str, ...],
        timeout_seconds: int | None = None,
    ) -> ShellOutcome:
        if not argv:
            raise ToolValidationError("argv must not be empty")
        command = Path(argv[0]).name
        if command not in self._allowed_commands:
            raise ToolPermissionError(f"command {command!r} is not in the allow-list")
        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout_seconds
        if timeout <= 0:
            raise ToolValidationError("timeout_seconds must be positive")
        workspace = _require_workspace(ctx)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise ToolExecutionError(f"command {command!r} timed out after {timeout}s") from exc
        return ShellOutcome(
            argv=argv,
            exit_code=max(proc.returncode or 0, 0),
            stdout=_decode_bounded(stdout_b, ctx.output_byte_cap),
            stderr=_decode_bounded(stderr_b, ctx.output_byte_cap),
        )


class LocalWorkspaceTestTool(TestTool):
    """Deterministic test-suite runner bound to ``ctx.workspace_path``."""

    def __init__(
        self,
        *,
        suites: dict[str, tuple[str, ...]] | None = None,
        default_timeout_seconds: int = _DEFAULT_TEST_TIMEOUT_SECONDS,
    ) -> None:
        self._suites = suites or {
            "python_lint": (sys.executable, "-m", "ruff", "check", "--"),
            "python_test": (sys.executable, "-m", "pytest", "--"),
            "typescript_typecheck": ("npx", "tsc", "--noEmit", "--pretty", "false", "--"),
            "typescript_test": ("npx", "vitest", "run", "--globals", "--"),
        }
        if not self._suites:
            raise ValueError("suites must not be empty")
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive")
        self._default_timeout_seconds = default_timeout_seconds

    async def run(
        self,
        ctx: ToolContext,
        *,
        suite: str,
        targets: tuple[str, ...] = (),
        timeout_seconds: int | None = None,
    ) -> TestOutcome:
        if not suite:
            raise ToolValidationError("suite must not be empty")
        if suite not in self._suites:
            raise ToolPermissionError(f"test suite {suite!r} is not in the allow-list")
        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout_seconds
        if timeout <= 0:
            raise ToolValidationError("timeout_seconds must be positive")
        workspace = _require_workspace(ctx)
        argv = (*self._suites[suite], *(_suite_targets(workspace, suite, targets)))
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(workspace),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise ToolExecutionError(f"test suite {suite!r} timed out after {timeout}s") from exc
        return TestOutcome(
            suite=suite,
            argv=argv,
            exit_code=max(proc.returncode or 0, 0),
            stdout=_decode_bounded(stdout_b, ctx.output_byte_cap),
            stderr=_decode_bounded(stderr_b, ctx.output_byte_cap),
        )


def _atomic_write(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(target)


def _normalize_targets(workspace: Path, targets: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for target in targets:
        resolved = _resolve_within(workspace, target)
        out.append(resolved.relative_to(workspace.resolve()).as_posix())
    return tuple(out)


def _suite_targets(workspace: Path, suite: str, targets: tuple[str, ...]) -> tuple[str, ...]:
    if suite in {"python_test", "typescript_test"}:
        return ()
    return _normalize_targets(workspace, targets)


_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)


def _extract_diff_files(unified_diff: str) -> tuple[str, ...]:
    """Best-effort extraction of touched file paths from a unified diff."""

    files: list[str] = []
    for match in _DIFF_HEADER_RE.finditer(unified_diff):
        # Prefer the "b/" side (post-image); matches git apply's mental model.
        path = match.group(2)
        if path and path not in files:
            files.append(path)
    return tuple(files)


def _decode_bounded(raw: bytes, cap: int) -> str:
    return raw[:cap].decode("utf-8", errors="replace")
