"""Docker companion tool adapters for Phase β workspaces."""

from __future__ import annotations

import asyncio
import json
import re
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

_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_TEST_TIMEOUT_SECONDS = 120
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+?)$", re.MULTILINE)


def _decode_bounded(raw: bytes, cap: int) -> str:
    return raw[:cap].decode("utf-8", errors="replace")


def _extract_diff_files(unified_diff: str) -> tuple[str, ...]:
    files: list[str] = []
    seen: set[str] = set()
    for match in _DIFF_HEADER_RE.finditer(unified_diff):
        candidate = match.group("b") or match.group("a")
        if candidate not in seen:
            seen.add(candidate)
            files.append(candidate)
    return tuple(files)


class _DockerWorkspaceToolBase:
    """Common docker companion helpers shared across tool adapters."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        docker_executable: str = "docker",
        container_prefix: str = "meta-agent-ws",
        container_workspace_root: str = "/workspace",
        allowed_commands: frozenset[str] | None = None,
        default_timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._workspace_root = workspace_root.resolve()
        self._docker_executable = docker_executable
        self._container_prefix = container_prefix
        self._container_workspace_root = container_workspace_root.rstrip("/") or "/workspace"
        self._allowed_commands = allowed_commands or frozenset(
            {"ruff", "python", "python3", "pytest"}
        )
        if not self._allowed_commands:
            raise ValueError("allowed_commands must not be empty")
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive")
        self._default_timeout_seconds = default_timeout_seconds

    def _require_workspace(self, ctx: ToolContext) -> Path:
        if ctx.workspace_path is None:
            raise ToolPermissionError("tool requires a workspace but ctx.workspace_path is None")
        workspace = ctx.workspace_path.resolve()
        try:
            workspace.relative_to(self._workspace_root)
        except ValueError as exc:
            raise ToolPermissionError(
                f"workspace {workspace!s} is outside configured docker workspace root"
            ) from exc
        return workspace

    def _exec_prefix(self, workspace: Path, *, interactive: bool = False) -> list[str]:
        workspace_id = workspace.parent.name
        container_name = f"{self._container_prefix}-{workspace_id}"
        container_cwd = f"{self._container_workspace_root}/{workspace.name}"
        args = [
            self._docker_executable,
            "exec",
        ]
        if interactive:
            args.append("-i")
        args.extend(
            [
                "-w",
                container_cwd,
                container_name,
            ]
        )
        return args

    async def _exec(
        self,
        workspace: Path,
        *,
        argv: tuple[str, ...],
        timeout_seconds: int,
        stdin: bytes | None = None,
    ) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            *self._exec_prefix(workspace, interactive=stdin is not None),
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            communicate = proc.communicate(stdin) if stdin is not None else proc.communicate()
            stdout_b, stderr_b = await asyncio.wait_for(
                communicate, timeout=timeout_seconds
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            command = Path(argv[0]).name if argv else "command"
            raise ToolExecutionError(
                f"command {command!r} timed out after {timeout_seconds}s"
            ) from exc
        return (max(proc.returncode or 0, 0), stdout_b, stderr_b)

    @staticmethod
    def _relative_arg(workspace: Path, raw_path: str) -> str:
        if not raw_path:
            raise ToolValidationError("path must not be empty")
        candidate = (workspace / raw_path).resolve()
        try:
            rel = candidate.relative_to(workspace.resolve())
        except ValueError as exc:
            raise ToolPermissionError(f"path {raw_path!r} escapes the workspace") from exc
        return rel.as_posix()


class DockerWorkspaceFileSystemTool(_DockerWorkspaceToolBase, FileSystemTool):
    """Filesystem tool executed through the companion container."""

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
        workspace = self._require_workspace(ctx)
        rel = self._relative_arg(workspace, path)
        cap = ctx.output_byte_cap if max_bytes is None else min(ctx.output_byte_cap, max_bytes)
        if cap <= 0:
            raise ToolValidationError("max_bytes must be positive")
        script = (
            "from pathlib import Path\n"
            "import sys\n"
            "p=Path(sys.argv[1]); offset=int(sys.argv[2]); cap=int(sys.argv[3])\n"
            "if not p.is_file():\n"
            "    raise SystemExit('not-a-file')\n"
            "data=p.read_bytes()[offset:offset+cap]\n"
            "sys.stdout.write(data.decode('utf-8', errors='replace'))\n"
        )
        code, stdout_b, stderr_b = await self._exec(
            workspace,
            argv=("python3", "-c", script, rel, str(offset), str(cap)),
            timeout_seconds=self._default_timeout_seconds,
        )
        if code != 0:
            detail = _decode_bounded(stderr_b or stdout_b, ctx.output_byte_cap).strip() or "read failed"
            raise ToolExecutionError(f"read failed: {detail}")
        return _decode_bounded(stdout_b, ctx.output_byte_cap)

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
        workspace = self._require_workspace(ctx)
        rel = "." if not path else self._relative_arg(workspace, path)
        script = (
            "from pathlib import Path\n"
            "import json, sys\n"
            "target=Path(sys.argv[1]); recursive=(sys.argv[2]=='1'); max_entries=int(sys.argv[3]); cap=int(sys.argv[4])\n"
            "if not target.is_dir():\n"
            "    raise SystemExit('not-a-dir')\n"
            "iterator = target.rglob('*') if recursive else target.iterdir()\n"
            "out=[]; used=2\n"
            "for entry in sorted(iterator, key=lambda p: p.as_posix()):\n"
            "    rendered=entry.as_posix() + ('/' if entry.is_dir() else '')\n"
            "    sep=1 if out else 0\n"
            "    size=len(rendered.encode('utf-8'))\n"
            "    if used + sep + size > cap:\n"
            "        break\n"
            "    out.append(rendered)\n"
            "    used += sep + size\n"
            "    if len(out) >= max_entries:\n"
            "        break\n"
            "sys.stdout.write(json.dumps(out))\n"
        )
        code, stdout_b, stderr_b = await self._exec(
            workspace,
            argv=(
                "python3",
                "-c",
                script,
                rel,
                "1" if recursive else "0",
                str(max_entries),
                str(ctx.output_byte_cap),
            ),
            timeout_seconds=self._default_timeout_seconds,
        )
        if code != 0:
            detail = _decode_bounded(stderr_b or stdout_b, ctx.output_byte_cap).strip() or "list failed"
            raise ToolExecutionError(f"list_dir failed: {detail}")
        return tuple(json.loads(stdout_b.decode("utf-8", errors="replace")))

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
            re.compile(pattern)
        except re.error as exc:
            raise ToolValidationError(f"invalid regex: {exc}") from exc
        workspace = self._require_workspace(ctx)
        script = (
            "from pathlib import Path\n"
            "import json, re, sys\n"
            "pattern=sys.argv[1]\n"
            "globs=json.loads(sys.argv[2])\n"
            "max_matches=int(sys.argv[3]); cap=int(sys.argv[4])\n"
            "compiled=re.compile(pattern)\n"
            "hits=[]; seen=set(); used=2\n"
            "for glob in globs:\n"
            "    for entry in sorted(Path('.').glob(glob), key=lambda p: p.as_posix()):\n"
            "        if not entry.is_file() or entry in seen:\n"
            "            continue\n"
            "        seen.add(entry)\n"
            "        try:\n"
            "            text=entry.read_text(encoding='utf-8', errors='replace')\n"
            "        except OSError:\n"
            "            continue\n"
            "        for line_no, line in enumerate(text.splitlines(), start=1):\n"
            "            if compiled.search(line):\n"
            "                rendered=f'{entry.as_posix()}:{line_no}:{line}'\n"
            "                sep=1 if hits else 0\n"
            "                size=len(rendered.encode('utf-8'))\n"
            "                if used + sep + size > cap:\n"
            "                    sys.stdout.write(json.dumps(hits)); raise SystemExit(0)\n"
            "                hits.append({'path': entry.as_posix(), 'line_no': line_no, 'line': line})\n"
            "                used += sep + size\n"
            "                if len(hits) >= max_matches:\n"
            "                    sys.stdout.write(json.dumps(hits)); raise SystemExit(0)\n"
            "sys.stdout.write(json.dumps(hits))\n"
        )
        code, stdout_b, stderr_b = await self._exec(
            workspace,
            argv=(
                "python3",
                "-c",
                script,
                pattern,
                json.dumps(list(path_globs)),
                str(max_matches),
                str(ctx.output_byte_cap),
            ),
            timeout_seconds=self._default_timeout_seconds,
        )
        if code != 0:
            detail = _decode_bounded(stderr_b or stdout_b, ctx.output_byte_cap).strip() or "grep failed"
            raise ToolExecutionError(f"grep failed: {detail}")
        raw_hits = json.loads(stdout_b.decode("utf-8", errors="replace"))
        return tuple(GrepHit.model_validate(item) for item in raw_hits)


class DockerWorkspaceEditTool(_DockerWorkspaceToolBase, EditTool):
    """Edit tool executed through the companion container."""

    async def write(
        self,
        ctx: ToolContext,
        *,
        path: str,
        content: str,
    ) -> EditOutcome:
        workspace = self._require_workspace(ctx)
        rel = self._relative_arg(workspace, path)
        encoded = content.encode("utf-8")
        script = (
            "from pathlib import Path\n"
            "import json, sys\n"
            "target=Path(sys.argv[1])\n"
            "data=sys.stdin.buffer.read()\n"
            "target.parent.mkdir(parents=True, exist_ok=True)\n"
            "tmp=target.with_suffix(target.suffix + '.tmp')\n"
            "tmp.write_bytes(data)\n"
            "tmp.replace(target)\n"
            "sys.stdout.write(json.dumps({'files_changed':[sys.argv[1]], 'bytes_written': len(data)}))\n"
        )
        code, stdout_b, stderr_b = await self._exec(
            workspace,
            argv=("python3", "-c", script, rel),
            timeout_seconds=self._default_timeout_seconds,
            stdin=encoded,
        )
        if code != 0:
            detail = _decode_bounded(stderr_b or stdout_b, ctx.output_byte_cap).strip() or "write failed"
            raise ToolExecutionError(f"write failed: {detail}")
        payload = json.loads(stdout_b.decode("utf-8", errors="replace"))
        return EditOutcome(
            files_changed=tuple(str(item) for item in payload.get("files_changed", [])),
            bytes_written=int(payload.get("bytes_written", 0)),
        )

    async def patch_apply(
        self,
        ctx: ToolContext,
        *,
        unified_diff: str,
    ) -> EditOutcome:
        if not unified_diff.strip():
            raise ToolValidationError("unified_diff must not be empty")
        workspace = self._require_workspace(ctx)
        encoded = unified_diff.encode("utf-8")
        code, stdout_b, stderr_b = await self._exec(
            workspace,
            argv=("git", "apply", "--whitespace=nowarn", "-"),
            timeout_seconds=self._default_timeout_seconds,
            stdin=encoded,
        )
        if code != 0:
            detail = _decode_bounded(stderr_b or stdout_b, ctx.output_byte_cap).strip()
            raise ToolExecutionError(f"git apply failed: {detail or 'unknown error'}")
        return EditOutcome(
            files_changed=_extract_diff_files(unified_diff),
            bytes_written=len(encoded),
        )


class DockerWorkspaceShellTool(_DockerWorkspaceToolBase, ShellTool):
    """Run allow-listed commands via ``docker exec``."""

    async def run(
        self,
        ctx: ToolContext,
        *,
        argv: tuple[str, ...],
        timeout_seconds: int | None = None,
    ) -> ShellOutcome:
        if not argv:
            raise ToolValidationError("argv must not be empty")
        workspace = self._require_workspace(ctx)
        command = Path(argv[0]).name
        if command not in self._allowed_commands:
            raise ToolPermissionError(f"command {command!r} is not in the allow-list")
        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout_seconds
        if timeout <= 0:
            raise ToolValidationError("timeout_seconds must be positive")
        code, stdout_b, stderr_b = await self._exec(
            workspace,
            argv=argv,
            timeout_seconds=timeout,
        )
        return ShellOutcome(
            argv=argv,
            exit_code=code,
            stdout=_decode_bounded(stdout_b, ctx.output_byte_cap),
            stderr=_decode_bounded(stderr_b, ctx.output_byte_cap),
        )


class DockerWorkspaceTestTool(_DockerWorkspaceToolBase, TestTool):
    """Deterministic test-suite runner executed through the companion container."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        docker_executable: str = "docker",
        container_prefix: str = "meta-agent-ws",
        container_workspace_root: str = "/workspace",
        suites: dict[str, tuple[str, ...]] | None = None,
        default_timeout_seconds: int = _DEFAULT_TEST_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            workspace_root=workspace_root,
            docker_executable=docker_executable,
            container_prefix=container_prefix,
            container_workspace_root=container_workspace_root,
            default_timeout_seconds=default_timeout_seconds,
        )
        self._suites = suites or {
            "python_lint": ("python3", "-m", "ruff", "check", "--"),
            "python_test": ("python3", "-m", "pytest", "--"),
            "typescript_typecheck": ("npx", "tsc", "--noEmit", "--pretty", "false", "--"),
            "typescript_test": ("npx", "vitest", "run", "--globals", "--"),
        }
        if not self._suites:
            raise ValueError("suites must not be empty")

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
        workspace = self._require_workspace(ctx)
        argv = (
            *self._suites[suite],
            *(
                self._relative_arg(workspace, target)
                for target in _suite_targets(suite, targets)
            ),
        )
        code, stdout_b, stderr_b = await self._exec(
            workspace,
            argv=tuple(argv),
            timeout_seconds=timeout,
        )
        return TestOutcome(
            suite=suite,
            argv=tuple(argv),
            exit_code=code,
            stdout=_decode_bounded(stdout_b, ctx.output_byte_cap),
            stderr=_decode_bounded(stderr_b, ctx.output_byte_cap),
        )


def _suite_targets(suite: str, targets: tuple[str, ...]) -> tuple[str, ...]:
    if suite in {"python_test", "typescript_test"}:
        return ()
    return targets
