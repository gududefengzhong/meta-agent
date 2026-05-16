"""Single-host git worktree adapter for :class:`WorkspaceManager`.

Provisions one isolated workspace per task by either cloning an
upstream repo or initialising a fresh local one, then attaching a
``git worktree`` on a dedicated feature branch. The feature branch
is the only writable surface; the base ref stays untouched in the
sibling ``main`` directory.

Layout under ``root_dir`` (per workspace)::

    {workspace_id}/
        main/      # the clone (or fresh init); HEAD = base ref
        feature/   # the worktree; HEAD = feature branch
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import shutil
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from meta_agent.core.domain.workspace import Workspace
from meta_agent.core.ports.workspace import WorkspaceError, WorkspaceManager

_CREDENTIAL_PATTERN = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]*@")


def _redact_credentials(text: str) -> str:
    """Strip ``user:pass`` from URLs so error / log surfaces stay safe."""
    return _CREDENTIAL_PATTERN.sub(r"\g<scheme><redacted>@", text)


@dataclass(frozen=True, slots=True)
class LocalGitConfig:
    """Static configuration for the local-git workspace adapter."""

    root_dir: Path
    git_executable: str = "git"
    clone_depth: int | None = 1
    timeout_seconds: float = 300.0


class LocalGitWorkspaceManager(WorkspaceManager):
    """Materialise workspaces by shelling out to the local ``git`` CLI."""

    def __init__(
        self,
        config: LocalGitConfig,
        *,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._id_factory = id_factory or (lambda: f"ws-{uuid.uuid4().hex[:12]}")
        self._clock = clock or (lambda: datetime.now(UTC))

    async def provision(
        self,
        *,
        tenant_id: str,
        task_id: str,
        trace_id: str,
        branch: str,
        repo_url: str | None = None,
        base_ref: str | None = None,
    ) -> Workspace:
        workspace_id = self._id_factory()
        root = self._config.root_dir / workspace_id
        clone_dir = root / "main"
        worktree_dir = root / "feature"
        root.mkdir(parents=True, exist_ok=False)
        try:
            if repo_url is not None:
                await self._clone(repo_url, base_ref, clone_dir)
            else:
                await self._init_empty(clone_dir)
            await self._add_worktree(clone_dir, worktree_dir, branch, base_ref, repo_url)
            return Workspace(
                workspace_id=workspace_id,
                tenant_id=tenant_id,
                task_id=task_id,
                trace_id=trace_id,
                repo_url=repo_url,
                base_ref=base_ref,
                branch=branch,
                worktree_path=str(worktree_dir.resolve()),
                created_at=self._clock(),
            )
        except BaseException:
            shutil.rmtree(root, ignore_errors=True)
            raise

    async def cleanup(self, workspace: Workspace) -> None:
        worktree = Path(workspace.worktree_path)
        root = worktree.parent
        root_resolved = root.resolve()
        configured_root = self._config.root_dir.resolve()
        if not root_resolved.is_relative_to(configured_root):
            raise WorkspaceError(f"refusing to clean path outside configured root: {root_resolved}")
        if not root.exists():
            return  # idempotent: already cleaned up
        clone_dir = root / "main"
        if clone_dir.exists():
            # Graceful path; the rmtree below is the source of truth, so a
            # failed ``worktree remove`` (e.g. stale lock) must not block cleanup.
            with contextlib.suppress(WorkspaceError):
                await self._run(
                    [
                        self._config.git_executable,
                        "-C",
                        str(clone_dir),
                        "worktree",
                        "remove",
                        "--force",
                        str(worktree),
                    ]
                )
        try:
            shutil.rmtree(root)
        except OSError as exc:
            raise WorkspaceError(f"failed to remove workspace root {root}: {exc}") from exc

    async def _clone(self, repo_url: str, base_ref: str | None, dest: Path) -> None:
        args: list[str] = [self._config.git_executable, "clone"]
        if self._config.clone_depth is not None:
            args += ["--depth", str(self._config.clone_depth)]
        if base_ref is not None:
            args += ["--branch", base_ref]
        args += [repo_url, str(dest)]
        await self._run(args)

    async def _init_empty(self, dest: Path) -> None:
        git = self._config.git_executable
        await self._run([git, "init", str(dest)])
        # ``user.name`` and ``user.email`` are local to this repo; they
        # never reach a remote because the no-repo path has no remote.
        await self._run([git, "-C", str(dest), "config", "user.email", "agent@meta-agent.local"])
        await self._run([git, "-C", str(dest), "config", "user.name", "meta-agent"])
        await self._run([git, "-C", str(dest), "commit", "--allow-empty", "-m", "workspace init"])

    async def _add_worktree(
        self,
        clone_dir: Path,
        worktree_dir: Path,
        branch: str,
        base_ref: str | None,
        repo_url: str | None,
    ) -> None:
        args: list[str] = [
            self._config.git_executable,
            "-C",
            str(clone_dir),
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree_dir),
        ]
        # base_ref is meaningful only when we cloned from an upstream;
        # in the self-init case the ref name does not exist in the
        # fresh repo, so we branch off the synthetic HEAD instead.
        if base_ref is not None and repo_url is not None:
            args.append(base_ref)
        await self._run(args)

    async def _run(self, args: Sequence[str]) -> None:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=self._config.timeout_seconds
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise WorkspaceError(
                f"git command timed out after {self._config.timeout_seconds}s: "
                f"{_redact_credentials(' '.join(args))}"
            ) from None
        if proc.returncode != 0:
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
            stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
            cmd = _redact_credentials(" ".join(args))
            detail = _redact_credentials(stderr or stdout or "no output")
            raise WorkspaceError(f"git command failed (exit={proc.returncode}): {cmd}\n{detail}")
