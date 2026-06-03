"""Integration smokes for ``builtin.bug_fix`` on the Docker backend.

These tests use the real Postgres/Redis adapters plus the worker loop,
but keep the LLM deterministic via :class:`FakeLLMClient`.

Scope:

* Exercise the default ``TaskType.BUG_FIX -> builtin.bug_fix`` route
  when tool capabilities are present.
* Provision a Docker-backed workspace, execute the tool stack inside the
  companion container, and persist the final :class:`TaskResult`.
* Cover one Python repo and one TypeScript repo (``vitest`` suite).
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
from meta_agent.core.orchestration.graphs import BUG_FIX_GRAPH_ID
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
_TENANT = "tenant-bugfix"
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
    (repo / "src" / "greet.py").write_text(
        'def greet(name: str) -> str:\n    return f"hi {name}"\n',
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_greet.py").write_text(
        "from src.greet import greet\n\n\n"
        "def test_greet_adds_punctuation() -> None:\n"
        "    assert greet('Ada') == 'hi Ada!'\n",
        encoding="utf-8",
    )
    _run("git", "-C", str(repo), "add", ".")
    _run("git", "-C", str(repo), "commit", "-m", "initial")
    return repo


def _make_typescript_repo(root: Path) -> Path:
    repo = root / "typescript-repo"
    repo.mkdir()
    _run("git", "init", "--initial-branch=main", str(repo))
    _run("git", "-C", str(repo), "config", "user.email", "t@example.com")
    _run("git", "-C", str(repo), "config", "user.name", "test")
    (repo / "src").mkdir()
    (repo / "src" / "greet.ts").write_text(
        "export const greet = (name: string): string => `hi ${name}`;\n",
        encoding="utf-8",
    )
    (repo / "greet.test.ts").write_text(
        "describe('greet', () => {\n"
        "  it('adds punctuation', async () => {\n"
        "    const mod = await import('./src/greet');\n"
        "    expect(mod.greet('Ada')).toBe('hi Ada!');\n"
        "  });\n"
        "});\n",
        encoding="utf-8",
    )
    (repo / "tsconfig.json").write_text(
        "{\n"
        '  "compilerOptions": {\n'
        '    "target": "ES2020",\n'
        '    "module": "ESNext",\n'
        '    "moduleResolution": "Node",\n'
        '    "strict": true\n'
        "  },\n"
        '  "include": ["src/**/*.ts", "greet.test.ts"]\n'
        "}\n",
        encoding="utf-8",
    )
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
    assert registry.resolve(TaskType.BUG_FIX).graph_id == BUG_FIX_GRAPH_ID
    return registry


def _workspace_manager(root: Path, image: str) -> DockerWorkspaceManager:
    return DockerWorkspaceManager(
        DockerWorkspaceConfig(
            local_git=LocalGitConfig(root_dir=root),
            image=image,
        )
    )


async def _run_bug_fix_task(
    *,
    db_pool: DatabasePool,
    redis_client: Redis,
    registry: GraphRegistry,
    workspaces: DockerWorkspaceManager,
    payload: dict[str, object],
) -> tuple[Task, OutboxEvent, PgTaskRepository, PgOutboxRepository]:
    task_id = f"bugfix-{uuid.uuid4().hex[:8]}"
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
        task_type=TaskType.BUG_FIX,
        graph_id=None,
        state=TaskState.PENDING,
        input_payload=payload,
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
        payload=dict(payload),
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

    group = f"bugfix-workers-{uuid.uuid4().hex[:6]}"
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
    return task, event, task_repo, outbox_repo


async def test_bug_fix_python_repo_end_to_end_in_docker_backend(
    db_pool: DatabasePool,
    redis_client: Redis,
    tmp_path: Path,
    workspace_image: str,
) -> None:
    repo = _make_python_repo(tmp_path)
    patched = 'def greet(name: str) -> str:\n    return f"hi {name}!"\n'
    llm = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="edit_write",
                        arguments={"path": "src/greet.py", "content": patched},
                    ),
                ),
                finish_reason="tool_call",
            ),
            make_response(content="fixed python greeting"),
        ]
    )
    workspace_root = tmp_path / "workspaces"
    registry = _registry_for_docker_workspace(llm, workspace_root)
    task, event, task_repo, outbox_repo = await _run_bug_fix_task(
        db_pool=db_pool,
        redis_client=redis_client,
        registry=registry,
        workspaces=_workspace_manager(workspace_root, workspace_image),
        payload={
            "issue_description": "greet should add a punctuation mark",
            "target_files": ["src/greet.py"],
            "repo_url": str(repo),
            "base_ref": "main",
        },
    )

    ctx = RequestContext(
        tenant_id=task.tenant_id,
        principal_id=task.principal_id,
        trace_id=task.trace_id,
        request_id=task.task_id,
    )
    with bind_context(ctx):
        result = await task_repo.get_result(task.tenant_id, task.task_id)
        fetched = await task_repo.get(task.tenant_id, task.task_id)
    assert result is not None
    assert result.status == "succeeded"
    assert result.graph_id == BUG_FIX_GRAPH_ID
    assert result.output is not None
    output = result.output
    assert output["verifier_passed"] is True
    assert output["files_changed"] == ["src/greet.py"]
    assert output["push_skip_reason"] == "no_token"
    assert "suite=python_test" in output["verifier_output"]
    assert "diff --git a/src/greet.py b/src/greet.py" in output["patch"]
    assert fetched is not None and fetched.state == TaskState.SUCCEEDED
    assert (repo / "src" / "greet.py").read_text(
        encoding="utf-8"
    ) == 'def greet(name: str) -> str:\n    return f"hi {name}"\n'
    assert not any(workspace_root.iterdir()), "workspace root should be cleaned after run"
    relayed = await outbox_repo.get(event.event_id)
    assert relayed is not None and relayed.status == OutboxStatus.DISPATCHED


async def test_bug_fix_typescript_repo_end_to_end_in_docker_backend(
    db_pool: DatabasePool,
    redis_client: Redis,
    tmp_path: Path,
    workspace_image: str,
) -> None:
    repo = _make_typescript_repo(tmp_path)
    patched = "export const greet = (name: string): string => `hi ${name}!`;\n"
    llm = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(
                    ToolCall(
                        id="c1",
                        name="edit_write",
                        arguments={"path": "src/greet.ts", "content": patched},
                    ),
                ),
                finish_reason="tool_call",
            ),
            make_response(content="fixed typescript greeting"),
        ]
    )
    workspace_root = tmp_path / "workspaces"
    registry = _registry_for_docker_workspace(llm, workspace_root)
    task, event, task_repo, outbox_repo = await _run_bug_fix_task(
        db_pool=db_pool,
        redis_client=redis_client,
        registry=registry,
        workspaces=_workspace_manager(workspace_root, workspace_image),
        payload={
            "issue_description": "greet should add a punctuation mark",
            "target_files": ["src/greet.ts"],
            "repo_url": str(repo),
            "base_ref": "main",
            "verify_suite": "typescript_test",
        },
    )

    ctx = RequestContext(
        tenant_id=task.tenant_id,
        principal_id=task.principal_id,
        trace_id=task.trace_id,
        request_id=task.task_id,
    )
    with bind_context(ctx):
        result = await task_repo.get_result(task.tenant_id, task.task_id)
        fetched = await task_repo.get(task.tenant_id, task.task_id)
    assert result is not None
    assert result.status == "succeeded"
    assert result.graph_id == BUG_FIX_GRAPH_ID
    assert result.output is not None
    output = result.output
    assert output["verifier_passed"] is True
    assert output["files_changed"] == ["src/greet.ts"]
    assert output["push_skip_reason"] == "no_token"
    assert "suite=typescript_test" in output["verifier_output"]
    assert fetched is not None and fetched.state == TaskState.SUCCEEDED
    assert (repo / "src" / "greet.ts").read_text(
        encoding="utf-8"
    ) == "export const greet = (name: string): string => `hi ${name}`;\n"
    assert not any(workspace_root.iterdir()), "workspace root should be cleaned after run"
    relayed = await outbox_repo.get(event.event_id)
    assert relayed is not None and relayed.status == OutboxStatus.DISPATCHED
