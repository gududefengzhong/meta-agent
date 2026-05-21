"""Docker-backed workspace adapter.

The backend still provisions a host-side ``git worktree`` first, then
starts a companion container with that workspace bind-mounted into it.
Tool adapters can target either the host path or the container, but the
workspace lifecycle itself stays the same:

* host clone/worktree materialisation is still delegated to
  :class:`LocalGitWorkspaceManager`
* each workspace gets a long-lived companion container
* cleanup tears down the container before removing the host worktree

This keeps the graph-visible ``Workspace.worktree_path`` as a host path
while allowing Docker-backed FS/Edit/Shell/Test tools to execute inside
the container.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from meta_agent.core.domain.workspace import Workspace
from meta_agent.core.ports.workspace import WorkspaceError, WorkspaceManager
from meta_agent.infra.workspace.local_git import LocalGitConfig, LocalGitWorkspaceManager


@dataclass(frozen=True, slots=True)
class DockerWorkspaceConfig:
    """Static configuration for :class:`DockerWorkspaceManager`."""

    local_git: LocalGitConfig
    image: str
    docker_executable: str = "docker"
    container_prefix: str = "meta-agent-ws"
    network: str | None = None
    timeout_seconds: float = 120.0


class DockerWorkspaceManager(WorkspaceManager):
    """Provision host worktrees and pair them with dedicated containers."""

    def __init__(
        self,
        config: DockerWorkspaceConfig,
        *,
        inner: LocalGitWorkspaceManager | None = None,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        shared_id_factory = id_factory or (lambda: f"ws-{uuid.uuid4().hex[:12]}")
        shared_clock = clock or (lambda: datetime.now(UTC))
        self._config = config
        self._inner = inner or LocalGitWorkspaceManager(
            config.local_git,
            id_factory=shared_id_factory,
            clock=shared_clock,
        )
        self._containers: dict[str, str] = {}

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
        workspace = await self._inner.provision(
            tenant_id=tenant_id,
            task_id=task_id,
            trace_id=trace_id,
            branch=branch,
            repo_url=repo_url,
            base_ref=base_ref,
        )
        container_name = f"{self._config.container_prefix}-{workspace.workspace_id}"
        try:
            await self._start_container(workspace, container_name)
        except BaseException:
            await self._safe_inner_cleanup(workspace)
            raise
        self._containers[workspace.workspace_id] = container_name
        return workspace

    async def cleanup(self, workspace: Workspace) -> None:
        container_name = self._containers.pop(
            workspace.workspace_id,
            f"{self._config.container_prefix}-{workspace.workspace_id}",
        )
        with_context = False
        try:
            await self._run(
                [
                    self._config.docker_executable,
                    "rm",
                    "-f",
                    container_name,
                ]
            )
            with_context = True
        except WorkspaceError as exc:
            if not self._is_missing_container(str(exc)):
                raise
        finally:
            await self._inner.cleanup(workspace)
        # suppress "unused" concerns in the normal success path while
        # making the intention explicit for readers.
        if with_context:
            return

    async def _safe_inner_cleanup(self, workspace: Workspace) -> None:
        try:
            await self._inner.cleanup(workspace)
        except WorkspaceError:
            return

    async def _start_container(self, workspace: Workspace, container_name: str) -> None:
        worktree = Path(workspace.worktree_path).resolve()
        root = worktree.parent.resolve()
        args: list[str] = [
            self._config.docker_executable,
            "run",
            "-d",
            "--rm",
            "--name",
            container_name,
            "-v",
            f"{root}:/workspace",
            "-w",
            f"/workspace/{worktree.name}",
        ]
        if self._config.network is not None:
            args.extend(["--network", self._config.network])
        args.extend([self._config.image, "sleep", "infinity"])
        await self._run(args)

    async def _run(self, args: Sequence[str]) -> str:
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
                f"docker command timed out after {self._config.timeout_seconds}s: {' '.join(args)}"
            ) from None
        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            raise WorkspaceError(
                f"docker command failed (exit={proc.returncode}): {' '.join(args)}\n"
                f"{stderr or stdout or 'no output'}"
            )
        return stdout

    @staticmethod
    def _is_missing_container(message: str) -> bool:
        lowered = message.lower()
        return "no such container" in lowered or "cannot remove container" in lowered
