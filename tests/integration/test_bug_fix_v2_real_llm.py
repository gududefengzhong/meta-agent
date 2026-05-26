"""Dogfood-style integration test: ``bug_fix_v2`` against a real LLM.

Sibling of :mod:`test_bug_fix_v2_docker_smoke` but swaps
:class:`FakeLLMClient` for a real :class:`OpenRouterClient`. Used to
**evaluate** the agent on a non-trivial bug we constructed — does the
agent actually read the failing test, understand the contract, and
patch the function correctly?

The bug
=======
``src/discount.py`` has a ``discount_price(price, discount_percent)``
function that **does not validate input**:

* Negative ``discount_percent`` silently *increases* the price (returns
  a value larger than ``price`` — clearly wrong)
* ``discount_percent > 100`` produces a negative price (also wrong)

The pytest suite has two failing tests
(``test_discount_negative_raises`` / ``test_discount_over_100_raises``)
that expect ``ValueError`` mentioning ``discount_percent``. Three other
tests (normal / zero / full discount) pass on the buggy code so the
agent has to be careful not to break them.

What we assert
==============
We assert the **harness contract**, not the exact patch text:

* Task lands in ``SUCCEEDED`` state (or graceful ``FAILED`` with output)
* Result contains a ``files_changed`` list, ``verifier_output``, etc.
* If verifier passes, all 5 tests should pass (we also re-run pytest
  on the post-patch repo to confirm)

Pass/fail of the *agent quality* is reported as a single line in the
test's stdout summary, not as a hard assertion — a 0% baseline is
still useful signal for the dogfood narrative.

Cost / runtime
==============
~30s–3min wallclock; ~$0.05–0.50 OpenRouter token spend per run with
``deepseek/deepseek-chat`` (or whatever ``OPENROUTER_MODEL`` overrides
it to). Skipped automatically when ``OPENROUTER_API_KEY`` is not set
so unit-CI doesn't accidentally burn tokens.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from redis.asyncio import Redis

from meta_agent.core.domain.outbox import OutboxEvent
from meta_agent.core.domain.task import Task, TaskState, TaskType
from meta_agent.core.orchestration import GraphRegistry
from meta_agent.core.orchestration.graphs import BUG_FIX_V2_GRAPH_ID
from meta_agent.core.ports.llm import LLMClient
from meta_agent.infra.llm.config import OpenRouterConfig
from meta_agent.infra.llm.metered import MeteredLLMClient
from meta_agent.infra.llm.openrouter import OpenRouterClient
from meta_agent.infra.llm.redacting import RedactingLLMClient
from meta_agent.infra.persistence import (
    DatabasePool,
    OutboxDispatcher,
    PgAuditRepository,
    PgCheckpointRepository,
    PgLLMUsageRepository,
    PgOutboxRepository,
    PgTaskRepository,
)
from meta_agent.infra.queue import RedisStreamConsumer, RedisStreamPublisher
from meta_agent.infra.redaction import Redactor
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
from tests.core.orchestration._fakes import fake_deps

pytestmark = [pytest.mark.integration, pytest.mark.real_llm]

_WORKSPACE_IMAGE_ENV = "META_AGENT_WORKSPACE_IMAGE"
_DEFAULT_WORKSPACE_IMAGE = "meta-agent:local"

# Project-chosen baseline model. Override with OPENROUTER_MODEL env.
_DEFAULT_MODEL = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4-pro")


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=True, capture_output=True, text=True)


def _image_available(name: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", name],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


@pytest.fixture(scope="session")
def workspace_image() -> str:
    image = os.environ.get(_WORKSPACE_IMAGE_ENV, _DEFAULT_WORKSPACE_IMAGE)
    if not _image_available(image):
        pytest.skip(
            f"docker workspace image {image!r} not built; "
            "run `docker compose build` or `docker build -t meta-agent:local .` first",
        )
    return image


@pytest.fixture(scope="session")
def real_llm_client() -> LLMClient:
    api_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not api_key:
        # Try loading from <repo>/.env without taking a python-dotenv dep.
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("OPENROUTER_API_KEY=") and not line.startswith("#"):
                    api_key = line.split("=", 1)[1].strip().strip("'").strip('"')
                    break
    if not api_key:
        pytest.skip(
            "OPENROUTER_API_KEY not set in env or <repo>/.env; set it to run real-LLM dogfood test"
        )
    config = OpenRouterConfig(api_key=api_key, default_model=_DEFAULT_MODEL)
    inner: LLMClient = OpenRouterClient(config)
    return RedactingLLMClient(inner, redactor=Redactor())


def _make_discount_bug_repo(root: Path) -> Path:
    """Construct a small Python project with a real validation bug.

    Layout::

        discount-fixture/
          src/
            __init__.py
            discount.py
          tests/
            __init__.py
            test_discount.py
          pyproject.toml

    Bug: ``discount_price`` accepts negative and >100 ``discount_percent``
    without raising — two of the five pytest tests fail.
    """

    repo = root / "discount-fixture"
    repo.mkdir()
    _run("git", "init", "--initial-branch=main", str(repo))
    _run("git", "-C", str(repo), "config", "user.email", "fixture@example.com")
    _run("git", "-C", str(repo), "config", "user.name", "fixture")

    (repo / "src").mkdir()
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "discount.py").write_text(
        '''"""Pricing utilities."""


def discount_price(price: float, discount_percent: float) -> float:
    """Apply a percentage discount to ``price``.

    Examples:
        >>> discount_price(100.0, 20.0)
        80.0
        >>> discount_price(100.0, 0.0)
        100.0
    """
    return price - (price * discount_percent / 100)
''',
        encoding="utf-8",
    )

    (repo / "tests").mkdir()
    (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_discount.py").write_text(
        '''import pytest

from src.discount import discount_price


def test_discount_normal() -> None:
    assert discount_price(100.0, 20.0) == 80.0


def test_discount_zero() -> None:
    assert discount_price(100.0, 0.0) == 100.0


def test_discount_full() -> None:
    assert discount_price(100.0, 100.0) == 0.0


def test_discount_negative_raises() -> None:
    """Negative discount_percent is invalid input."""
    with pytest.raises(ValueError, match="discount_percent"):
        discount_price(100.0, -10.0)


def test_discount_over_100_raises() -> None:
    """discount_percent above 100 is invalid input."""
    with pytest.raises(ValueError, match="discount_percent"):
        discount_price(100.0, 150.0)
''',
        encoding="utf-8",
    )

    (repo / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=61.0"]\nbuild-backend = "setuptools.build_meta"\n\n'
        '[project]\nname = "discount-fixture"\nversion = "0.0.1"\nrequires-python = ">=3.10"\n',
        encoding="utf-8",
    )

    _run("git", "-C", str(repo), "add", ".")
    _run(
        "git", "-C", str(repo), "commit", "-m", "initial: discount module with input-validation bug"
    )
    return repo


def _registry_for_docker_workspace(llm: LLMClient, workspace_root: Path) -> GraphRegistry:
    tool_registry, tool_executor = build_local_tool_stack(
        fs=DockerWorkspaceFileSystemTool(workspace_root=workspace_root),
        edit=DockerWorkspaceEditTool(workspace_root=workspace_root),
        shell=DockerWorkspaceShellTool(workspace_root=workspace_root),
        test=DockerWorkspaceTestTool(workspace_root=workspace_root),
    )
    # fake_deps wires in an InMemoryPromptRegistry seeded with
    # BUILTIN_PROMPT_SEEDS — bug_fix_v2 hard-requires deps.prompt_registry,
    # so we can't pass a raw GraphDeps here.
    registry = build_registry(
        fake_deps(llm, tool_registry=tool_registry, tool_executor=tool_executor)
    )
    assert registry.resolve(TaskType.BUG_FIX).graph_id == BUG_FIX_V2_GRAPH_ID
    return registry


def _workspace_manager(root: Path, image: str) -> DockerWorkspaceManager:
    return DockerWorkspaceManager(
        DockerWorkspaceConfig(
            local_git=LocalGitConfig(root_dir=root),
            image=image,
        )
    )


_TOPIC = "task.commands"
_TENANT = "tenant-real-llm"
_PRINCIPAL = "dogfood"


async def _run_bug_fix_task(
    *,
    db_pool: DatabasePool,
    redis_client: Redis,
    registry: GraphRegistry,
    workspaces: DockerWorkspaceManager,
    payload: dict[str, object],
    llm_usage: PgLLMUsageRepository | None = None,
) -> tuple[Task, OutboxEvent, PgTaskRepository, PgOutboxRepository]:
    """Insert a task, enqueue via outbox, dispatch to redis, run worker once.

    Mirrors the pattern in :mod:`test_bug_fix_v2_docker_smoke` but with a
    longer block_ms so the real LLM has time to think; ``run_once`` consumes
    one task at most so the test stays single-shot.
    """

    task_id = f"real-llm-{uuid.uuid4().hex[:8]}"
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

    group = f"real-llm-workers-{uuid.uuid4().hex[:6]}"
    worker = WorkerLoop(
        stream=RedisStreamConsumer(
            redis_client,
            topic=_TOPIC,
            group=group,
            consumer_name="real-llm-worker-1",
            batch_size=1,
            block_ms=500,
        ),
        tasks=task_repo,
        checkpoints=PgCheckpointRepository(db_pool),
        audits=PgAuditRepository(db_pool),
        registry=registry,
        workspaces=workspaces,
        llm_usage=llm_usage,
        config=WorkerConfig(max_attempts=1, block_ms=500),
    )
    assert await worker.run_once() == 1
    return task, event, task_repo, outbox_repo


@pytest.mark.asyncio
async def test_bug_fix_v2_real_llm_discount_validation_bug(
    db_pool: DatabasePool,
    redis_client: Redis,
    tmp_path: Path,
    workspace_image: str,
    real_llm_client: LLMClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Real-LLM dogfood: agent must add input validation to ``discount_price``.

    Asserts the *harness contract* (task succeeded or failed gracefully with
    structured output). Whether the agent's patch is correct is reported via
    stdout for human inspection, not via hard assertions — a 0% baseline is
    still useful signal.
    """

    repo = _make_discount_bug_repo(tmp_path)
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()

    usage_repo = PgLLMUsageRepository(db_pool)
    metered_llm = MeteredLLMClient(real_llm_client, usage_repo, provider="openrouter")
    registry = _registry_for_docker_workspace(metered_llm, workspace_root)

    task, _event, task_repo, _outbox_repo = await _run_bug_fix_task(
        db_pool=db_pool,
        redis_client=redis_client,
        registry=registry,
        workspaces=_workspace_manager(workspace_root, workspace_image),
        llm_usage=usage_repo,
        payload={
            "issue_description": (
                "discount_price() in src/discount.py needs to validate its "
                "input. Currently it accepts negative discount_percent "
                "(silently increases the price — wrong) and discount_percent "
                "> 100 (produces a negative price — also wrong).\n\n"
                "The pytest suite under tests/ has two failing tests that "
                "expect ValueError to be raised with a message containing "
                "'discount_percent'. Three other tests pass on the current "
                "code — your fix must not break them.\n\n"
                "Fix discount_price to validate its input."
            ),
            "target_files": ["src/discount.py"],
            "repo_url": str(repo),
            "base_ref": "main",
            # Use pytest (not just lint) as the verifier — for a bug fix we
            # actually want red→green on the failing tests, not lint clean.
            "verify_suite": "python_test",
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

    # ---- Diagnostic dump BEFORE asserting (so failures show why) ----
    async with db_pool.acquire() as conn:
        audit_rows = await conn.fetch(
            "SELECT action, payload, occurred_at FROM audit_events "
            "WHERE tenant_id=$1 AND task_id=$2 ORDER BY occurred_at",
            task.tenant_id,
            task.task_id,
        )
        usage_rows = await conn.fetch(
            "SELECT * FROM llm_usage_logs WHERE tenant_id=$1 ORDER BY created_at",
            task.tenant_id,
        )
    print("\n" + "=" * 72)
    print(f"  Audit events ({len(audit_rows)}):")
    for r in audit_rows[:40]:
        print(f"    {r['occurred_at']:%H:%M:%S} {r['action']}")
        payload_str = str(r["payload"])
        if "error" in payload_str.lower() or "fail" in payload_str.lower():
            print(f"      payload: {payload_str[:500]}")
    print(f"  LLM usage logs ({len(usage_rows)}):")
    for r in usage_rows[:10]:
        print(f"    {dict(r)}")
    print("=" * 72)

    # ---- Harness contract assertions (these MUST hold) ----
    assert fetched is not None
    assert fetched.state in {TaskState.SUCCEEDED, TaskState.FAILED}, (
        f"unexpected terminal state: {fetched.state}"
    )
    assert result is not None, "task produced no result row"
    assert result.graph_id == BUG_FIX_V2_GRAPH_ID
    assert usage_rows, "real LLM call produced no llm_usage_logs rows"

    # ---- Agent quality observations (printed, not asserted) ----
    output = result.output or {}
    verifier_passed = output.get("verifier_passed", False)
    files_changed = output.get("files_changed", [])
    verifier_output = output.get("verifier_output", "")
    push_skip_reason = output.get("push_skip_reason")

    # Re-run pytest on the patched tree from the host to get an independent
    # verdict (the agent ran pytest inside a container, then cleaned the
    # Docker worktree). The persisted unified diff is the stable artifact.
    patch_text = str(output.get("patch") or "")
    patch_apply_success = False
    if patch_text:
        patch_apply = subprocess.run(
            ["git", "-C", str(repo), "apply", "--index", "-"],
            input=patch_text,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        patch_apply_success = patch_apply.returncode == 0
    pytest_passed_count = 0
    pytest_failed_count = 0
    pytest_total = 5
    try:
        pytest_result = subprocess.run(
            ["python", "-m", "pytest", str(repo / "tests"), "-q", "--no-header"],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        # Quick tally from pytest exit summary
        for line in pytest_result.stdout.splitlines():
            if "passed" in line and "failed" in line:
                # e.g. "2 failed, 3 passed in 0.04s"
                parts = line.replace(",", "").split()
                for i, token in enumerate(parts):
                    if token == "passed" and i > 0:
                        pytest_passed_count = int(parts[i - 1])
                    if token == "failed" and i > 0:
                        pytest_failed_count = int(parts[i - 1])
            elif "passed" in line and "failed" not in line:
                # e.g. "5 passed in 0.04s"
                parts = line.replace(",", "").split()
                for i, token in enumerate(parts):
                    if token == "passed" and i > 0:
                        pytest_passed_count = int(parts[i - 1])
    except (subprocess.SubprocessError, ValueError):
        pass

    final_file = (repo / "src" / "discount.py").read_text(encoding="utf-8")

    # Print everything for the human reading the test log.
    print("\n" + "=" * 72)
    print("  Real-LLM bug_fix_v2 dogfood result")
    print("=" * 72)
    print(f"  Model:              {_DEFAULT_MODEL}")
    print(f"  Task state:         {fetched.state}")
    print(f"  Verifier passed:    {verifier_passed}")
    print(f"  Files changed:      {files_changed}")
    print(f"  Push skip reason:   {push_skip_reason}")
    print(f"  Patch replayed:     {patch_apply_success}")
    print(
        f"  Pytest (post-fix):  {pytest_passed_count}/{pytest_total} passed, {pytest_failed_count} failed"
    )
    print("-" * 72)
    print("  Verifier output (last 800 chars):")
    print(verifier_output[-800:] if verifier_output else "  (none)")
    print("-" * 72)
    print("  Final src/discount.py:")
    print(final_file)
    print("=" * 72)
    print()

    # Capsys plumbing: pytest -s prints these directly; otherwise the
    # block lands in the captured stdout. Either way the human reading
    # the test output sees the result.
    captured = capsys.readouterr()
    if captured.out:
        # Ensure pytest -q -s shows it
        import sys

        sys.stdout.write(captured.out)
