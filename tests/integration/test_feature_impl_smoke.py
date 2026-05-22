"""Integration smoke for ``builtin.feature_impl`` on the Docker backend.

Mirrors the bug_fix_v2 smoke pattern: real Postgres/Redis adapters and
the full worker dispatch loop, with the LLM kept deterministic via
:class:`FakeLLMClient`.

Scope (Phase β+ PR 1):

* Submit ``TaskType.FEATURE_IMPL`` and confirm it routes through
  ``builtin.feature_impl`` (not bug_fix_v2 or the bare shell_agent).
* Confirm the docker-backed tool surface (``edit_write`` + ``test_run``)
  executes inside the companion container and the graph reaches a
  ``SUCCEEDED`` terminal state.
* Confirm audit / outbox plumbing fires for the new graph id.

What this smoke deliberately does NOT cover:

* Deep verification that the produced ``add`` function passes pytest
  against the source repo — feature_impl does not commit or push, so
  the source tree stays untouched and the worktree is cleaned on exit.
  TypeScript coverage + repo persistence are deferred to a later β+
  PR once the feature_impl → auto_pr chain lands.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from redis.asyncio import Redis

from meta_agent.core.domain.outbox import OutboxEvent, OutboxStatus
from meta_agent.core.domain.task import Task, TaskState, TaskType
from meta_agent.core.orchestration import GraphRegistry
from meta_agent.core.orchestration.graphs import FEATURE_IMPL_GRAPH_ID
from meta_agent.core.ports.tools import ToolCall
from meta_agent.infra.persistence import (
    DatabasePool,
    OutboxDispatcher,
    PgAuditRepository,
    PgCheckpointRepository,
    PgOutboxRepository,
    PgTaskRepository,
)
from meta_agent.infra.queue import RedisStreamConsumer, RedisStreamPublisher
from meta_agent.infra.security.context import RequestContext, bind_context
from meta_agent.infra.tools import (
    DockerWorkspaceEditTool,
    DockerWorkspaceFileSystemTool,
    DockerWorkspaceShellTool,
    DockerWorkspaceTestTool,
)
from meta_agent.infra.workspace import (
    DockerWorkspaceConfig,
    DockerWorkspaceManager,
    LocalGitConfig,
)
from meta_agent.worker.bootstrap import build_local_tool_stack, build_registry
from meta_agent.worker.runner import WorkerConfig, WorkerLoop
from tests.core.orchestration._fakes import (
    FakeLLMClient,
    fake_deps,
    make_response,
)

pytestmark = pytest.mark.integration

_TOPIC = "task.commands"
_TENANT = "tenant-feature-impl"
_PRINCIPAL = "system"
_WORKSPACE_IMAGE_ENV = "META_AGENT_TEST_WORKSPACE_IMAGE"
_DEFAULT_WORKSPACE_IMAGE = "meta-agent:local"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, capture_output=True, text=True)


def _image_available(name: str) -> bool:
    try:
        subprocess.run(
            ["docker", "image", "inspect", name],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return False
    return True


@pytest.fixture(scope="session")
def workspace_image() -> str:
    image = os.environ.get(_WORKSPACE_IMAGE_ENV, _DEFAULT_WORKSPACE_IMAGE)
    if not _image_available(image):
        pytest.skip(
            f"docker workspace image {image!r} is not available; build it before running this smoke",
            allow_module_level=False,
        )
    return image


def _make_python_repo(root: Path) -> Path:
    repo = root / "python-repo"
    repo.mkdir()
    _run("git", "init", "--initial-branch=main", str(repo))
    _run("git", "-C", str(repo), "config", "user.email", "t@example.com")
    _run("git", "-C", str(repo), "config", "user.name", "test")
    (repo / "src").mkdir()
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    _run("git", "-C", str(repo), "add", ".")
    _run("git", "-C", str(repo), "commit", "-m", "initial")
    return repo


def _registry_for_docker_workspace(llm: FakeLLMClient, workspace_root: Path) -> GraphRegistry:
    tool_registry, tool_executor = build_local_tool_stack(
        fs=DockerWorkspaceFileSystemTool(workspace_root=workspace_root),
        edit=DockerWorkspaceEditTool(workspace_root=workspace_root),
        shell=DockerWorkspaceShellTool(workspace_root=workspace_root),
        test=DockerWorkspaceTestTool(workspace_root=workspace_root),
    )
    registry = build_registry(
        fake_deps(
            llm,
            tool_registry=tool_registry,
            tool_executor=tool_executor,
        )
    )
    assert registry.resolve(TaskType.FEATURE_IMPL).graph_id == FEATURE_IMPL_GRAPH_ID
    return registry


def _workspace_manager(root: Path, image: str) -> DockerWorkspaceManager:
    return DockerWorkspaceManager(
        DockerWorkspaceConfig(
            local_git=LocalGitConfig(root_dir=root),
            image=image,
        )
    )


async def test_feature_impl_python_repo_end_to_end_in_docker_backend(
    db_pool: DatabasePool,
    redis_client: Redis,
    tmp_path: Path,
    workspace_image: str,
) -> None:
    repo = _make_python_repo(tmp_path)
    add_module = "def add(a: int, b: int) -> int:\n    return a + b\n"
    llm = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="edit_write",
                        arguments={"path": "src/calc.py", "content": add_module},
                    ),
                ),
                finish_reason="tool_call",
            ),
            make_response(content="implemented add(a, b)"),
        ]
    )
    workspace_root = tmp_path / "workspaces"
    registry = _registry_for_docker_workspace(llm, workspace_root)
    workspaces = _workspace_manager(workspace_root, workspace_image)

    task_id = f"featimpl-{uuid.uuid4().hex[:8]}"
    trace_id = f"trace-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)
    ctx = RequestContext(
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id=trace_id,
        request_id=task_id,
    )
    task = Task(
        task_id=task_id,
        tenant_id=_TENANT,
        principal_id=_PRINCIPAL,
        trace_id=trace_id,
        idempotency_key=f"idem-{task_id}",
        task_type=TaskType.FEATURE_IMPL,
        graph_id=None,
        state=TaskState.PENDING,
        input_payload={
            "user_prompt": "Implement add(a, b) that returns a + b in src/calc.py.",
            "target_files": ["src/calc.py"],
            "repo_url": str(repo),
            "base_ref": "main",
        },
        created_at=now,
        updated_at=now,
    )
    event = OutboxEvent(
        event_id=f"evt-{uuid.uuid4().hex[:8]}",
        tenant_id=_TENANT,
        trace_id=trace_id,
        aggregate_type="task",
        aggregate_id=task_id,
        topic=_TOPIC,
        payload=dict(task.input_payload),
        idempotency_key=f"idem-{task_id}",
        created_at=now,
    )
    task_repo = PgTaskRepository(db_pool)
    outbox_repo = PgOutboxRepository(db_pool)
    with bind_context(ctx):
        async with db_pool.transaction() as conn:
            await task_repo.upsert_in_conn(task, conn)
            await outbox_repo.enqueue_in_conn(event, conn)

    publisher = RedisStreamPublisher(redis_client)
    dispatcher = OutboxDispatcher(outbox_repo, publisher)
    assert await dispatcher.run_once() == 1

    group = f"featimpl-workers-{uuid.uuid4().hex[:6]}"
    worker = WorkerLoop(
        stream=RedisStreamConsumer(
            redis_client,
            topic=_TOPIC,
            group=group,
            consumer_name="worker-1",
            batch_size=4,
            block_ms=200,
        ),
        tasks=task_repo,
        checkpoints=PgCheckpointRepository(db_pool),
        audits=PgAuditRepository(db_pool),
        registry=registry,
        workspaces=workspaces,
        config=WorkerConfig(max_attempts=3, block_ms=200),
    )
    assert await worker.run_once() == 1

    with bind_context(ctx):
        result = await task_repo.get_result(task.tenant_id, task.task_id)
        fetched = await task_repo.get(task.tenant_id, task.task_id)
    assert result is not None
    assert result.status == "succeeded"
    assert result.graph_id == FEATURE_IMPL_GRAPH_ID
    assert result.output is not None
    output = result.output
    # shell_agent output shape (feature_impl reuses it verbatim)
    assert output["assistant_message"] == "implemented add(a, b)"
    assert output["steps"] == 2
    assert output["tool_invocations"] == 1
    assert output["truncated_by_max_steps"] is False
    assert fetched is not None and fetched.state == TaskState.SUCCEEDED
    # feature_impl does not commit/push; the source repo file stays untouched
    assert not (repo / "src" / "calc.py").exists()
    assert not any(workspace_root.iterdir()), "workspace root should be cleaned after run"
    relayed = await outbox_repo.get(event.event_id)
    assert relayed is not None and relayed.status == OutboxStatus.DISPATCHED
