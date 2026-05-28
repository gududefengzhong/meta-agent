"""Small real-LLM eval baseline for the Bug Fix CLI Agent.

This is deliberately not SWE-bench. It is a small, repo-local baseline
that answers whether the current ``bug_fix_v2`` loop can repeatedly
solve simple bug-fix tasks while producing comparable observability
signals: verifier result, attempts, tool calls, token/cost, and failure
classification.

The test asserts the harness contract, not pass@1. Quality signals are
printed as JSON so a human can paste them into the dogfood doc.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from redis.asyncio import Redis

from meta_agent.core.domain.outbox import OutboxEvent
from meta_agent.core.domain.task import Task, TaskState, TaskType
from meta_agent.core.orchestration import GraphRegistry
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
from meta_agent.infra.workspace import DockerWorkspaceManager
from meta_agent.worker.runner import WorkerConfig, WorkerLoop
from tests.integration.test_bug_fix_v2_real_llm import (
    _registry_for_docker_workspace,
    _workspace_manager,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_llm]

_WORKSPACE_IMAGE_ENV = "META_AGENT_WORKSPACE_IMAGE"
_DEFAULT_WORKSPACE_IMAGE = "meta-agent:local"
_DEFAULT_MODEL = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4-pro")


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    language: str
    issue: str
    target_files: tuple[str, ...]
    verify_suite: str
    make_repo: Callable[[Path], Path]


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
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("OPENROUTER_API_KEY=") and not line.startswith("#"):
                    api_key = line.split("=", 1)[1].strip().strip("'").strip('"')
                    break
    if not api_key:
        pytest.skip("OPENROUTER_API_KEY not set in env or <repo>/.env; set it to run real-LLM eval")
    config = OpenRouterConfig(api_key=api_key, default_model=_DEFAULT_MODEL)
    inner: LLMClient = OpenRouterClient(config)
    return RedactingLLMClient(inner, redactor=Redactor())


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _run("git", "init", "--initial-branch=main", str(repo))
    _run("git", "-C", str(repo), "config", "user.email", "eval@example.com")
    _run("git", "-C", str(repo), "config", "user.name", "eval")


def _commit(repo: Path) -> None:
    _run("git", "-C", str(repo), "add", ".")
    _run("git", "-C", str(repo), "commit", "-m", "initial eval fixture")


def _make_python_greeting_repo(root: Path) -> Path:
    repo = root / "python-greeting"
    _init_repo(repo)
    (repo / "src").mkdir()
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
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
    _commit(repo)
    return repo


def _make_python_discount_repo(root: Path) -> Path:
    repo = root / "python-discount"
    _init_repo(repo)
    (repo / "src").mkdir()
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "discount.py").write_text(
        "def discount_price(price: float, discount_percent: float) -> float:\n"
        "    return price - (price * discount_percent / 100)\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_discount.py").write_text(
        "import pytest\n\n"
        "from src.discount import discount_price\n\n\n"
        "def test_normal_discount() -> None:\n"
        "    assert discount_price(100.0, 20.0) == 80.0\n\n\n"
        "def test_negative_discount_raises() -> None:\n"
        "    with pytest.raises(ValueError, match='discount_percent'):\n"
        "        discount_price(100.0, -1.0)\n\n\n"
        "def test_over_100_discount_raises() -> None:\n"
        "    with pytest.raises(ValueError, match='discount_percent'):\n"
        "        discount_price(100.0, 101.0)\n",
        encoding="utf-8",
    )
    _commit(repo)
    return repo


def _make_python_tax_repo(root: Path) -> Path:
    repo = root / "python-tax"
    _init_repo(repo)
    (repo / "src").mkdir()
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "tax.py").write_text(
        "def total_with_tax(subtotal: float, tax_percent: float) -> float:\n"
        "    return subtotal + tax_percent\n",
        encoding="utf-8",
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "test_tax.py").write_text(
        "import pytest\n\n"
        "from src.tax import total_with_tax\n\n\n"
        "def test_percent_tax() -> None:\n"
        "    assert total_with_tax(100.0, 8.25) == 108.25\n\n\n"
        "def test_zero_tax() -> None:\n"
        "    assert total_with_tax(50.0, 0.0) == 50.0\n\n\n"
        "def test_negative_tax_rejected() -> None:\n"
        "    with pytest.raises(ValueError, match='tax_percent'):\n"
        "        total_with_tax(100.0, -1.0)\n",
        encoding="utf-8",
    )
    _commit(repo)
    return repo


def _make_typescript_greeting_repo(root: Path) -> Path:
    repo = root / "typescript-greeting"
    _init_repo(repo)
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
    (repo / "tsconfig.json").write_text(_tsconfig(), encoding="utf-8")
    _commit(repo)
    return repo


def _make_typescript_clamp_repo(root: Path) -> Path:
    repo = root / "typescript-clamp"
    _init_repo(repo)
    (repo / "src").mkdir()
    (repo / "src" / "clamp.ts").write_text(
        "export const clamp = (value: number, min: number, max: number): number => value;\n",
        encoding="utf-8",
    )
    (repo / "clamp.test.ts").write_text(
        "describe('clamp', () => {\n"
        "  it('keeps values inside range', async () => {\n"
        "    const mod = await import('./src/clamp');\n"
        "    expect(mod.clamp(5, 0, 10)).toBe(5);\n"
        "  });\n"
        "  it('raises low values to min', async () => {\n"
        "    const mod = await import('./src/clamp');\n"
        "    expect(mod.clamp(-2, 0, 10)).toBe(0);\n"
        "  });\n"
        "  it('lowers high values to max', async () => {\n"
        "    const mod = await import('./src/clamp');\n"
        "    expect(mod.clamp(12, 0, 10)).toBe(10);\n"
        "  });\n"
        "});\n",
        encoding="utf-8",
    )
    (repo / "tsconfig.json").write_text(_tsconfig(), encoding="utf-8")
    _commit(repo)
    return repo


def _tsconfig() -> str:
    return (
        "{\n"
        '  "compilerOptions": {\n'
        '    "target": "ES2020",\n'
        '    "module": "ESNext",\n'
        '    "moduleResolution": "Node",\n'
        '    "strict": true\n'
        "  },\n"
        '  "include": ["src/**/*.ts", "*.test.ts"]\n'
        "}\n"
    )


_CASES = (
    EvalCase(
        case_id="py_greeting_punctuation",
        language="python",
        issue="greet(name) should add an exclamation mark while preserving the existing greeting text.",
        target_files=("src/greet.py",),
        verify_suite="python_test",
        make_repo=_make_python_greeting_repo,
    ),
    EvalCase(
        case_id="py_discount_validation",
        language="python",
        issue=(
            "discount_price must reject discount_percent values below 0 or above 100 with "
            "ValueError mentioning discount_percent, without breaking valid discounts."
        ),
        target_files=("src/discount.py",),
        verify_suite="python_test",
        make_repo=_make_python_discount_repo,
    ),
    EvalCase(
        case_id="py_tax_percent",
        language="python",
        issue=(
            "total_with_tax currently adds the raw tax_percent. Treat tax_percent as a "
            "percentage, and reject negative tax_percent with ValueError mentioning tax_percent."
        ),
        target_files=("src/tax.py",),
        verify_suite="python_test",
        make_repo=_make_python_tax_repo,
    ),
    EvalCase(
        case_id="ts_greeting_punctuation",
        language="typescript",
        issue="greet(name) should add an exclamation mark while preserving the existing greeting text.",
        target_files=("src/greet.ts",),
        verify_suite="typescript_test",
        make_repo=_make_typescript_greeting_repo,
    ),
    EvalCase(
        case_id="ts_clamp_range",
        language="typescript",
        issue="clamp(value, min, max) should return min for low values, max for high values, and value inside range.",
        target_files=("src/clamp.ts",),
        verify_suite="typescript_test",
        make_repo=_make_typescript_clamp_repo,
    ),
)


async def _run_eval_task(
    *,
    db_pool: DatabasePool,
    redis_client: Redis,
    registry: GraphRegistry,
    workspaces: DockerWorkspaceManager,
    payload: dict[str, object],
    llm_usage: PgLLMUsageRepository,
    topic: str,
) -> tuple[Task, PgTaskRepository]:
    task_id = f"eval-{uuid.uuid4().hex[:8]}"
    trace_id = f"trace-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC)
    ctx = RequestContext(
        tenant_id="tenant-eval",
        principal_id="dogfood",
        trace_id=trace_id,
        request_id=task_id,
    )
    task = Task(
        task_id=task_id,
        tenant_id=ctx.tenant_id,
        principal_id=ctx.principal_id,
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
        tenant_id=ctx.tenant_id,
        trace_id=trace_id,
        aggregate_type="task",
        aggregate_id=task_id,
        topic=topic,
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

    worker = WorkerLoop(
        stream=RedisStreamConsumer(
            redis_client,
            topic=topic,
            group=f"eval-workers-{uuid.uuid4().hex[:6]}",
            consumer_name="eval-worker-1",
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
    return task, task_repo


@pytest.mark.asyncio
async def test_bug_fix_v2_real_llm_eval_baseline(
    db_pool: DatabasePool,
    redis_client: Redis,
    tmp_path: Path,
    workspace_image: str,
    real_llm_client: LLMClient,
    capsys: pytest.CaptureFixture[str],
) -> None:
    usage_repo = PgLLMUsageRepository(db_pool)
    audit_repo = PgAuditRepository(db_pool)
    metered_llm = MeteredLLMClient(real_llm_client, usage_repo, provider="openrouter")
    workspace_root = tmp_path / "workspaces"
    workspace_root.mkdir()
    registry = _registry_for_docker_workspace(
        metered_llm,
        workspace_root,
        audit_repo=audit_repo,
    )
    workspaces = _workspace_manager(workspace_root, workspace_image)

    rows: list[dict[str, Any]] = []
    for case in _CASES:
        repo = case.make_repo(tmp_path)
        task, task_repo = await _run_eval_task(
            db_pool=db_pool,
            redis_client=redis_client,
            registry=registry,
            workspaces=workspaces,
            llm_usage=usage_repo,
            topic=f"task.commands.eval.{case.case_id}.{uuid.uuid4().hex[:8]}",
            payload={
                "issue_description": case.issue,
                "target_files": list(case.target_files),
                "repo_url": str(repo),
                "base_ref": "main",
                "verify_suite": case.verify_suite,
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
        assert fetched is not None
        assert fetched.state in {TaskState.SUCCEEDED, TaskState.FAILED}
        assert result is not None
        output = result.output or {}
        usage = await _usage_summary(db_pool, task.tenant_id, task.task_id)
        audit = await _audit_summary(db_pool, task.tenant_id, task.task_id)
        failure = output.get("failure_explanation")
        failure_category = failure.get("category") if isinstance(failure, dict) else None
        verifier_passed = bool(output.get("verifier_passed", False))
        rows.append(
            {
                "case_id": case.case_id,
                "language": case.language,
                "task_state": fetched.state.value,
                "result_status": result.status,
                "verifier_passed": verifier_passed,
                "verifier_failed": not verifier_passed,
                "failure_category": failure_category,
                "files_changed": output.get("files_changed", []),
                "attempts": _int_or_zero(output.get("attempts")),
                "tool_invocations": _int_or_zero(output.get("tool_invocations")),
                "patch_present": bool(output.get("patch")),
                **usage,
                **audit,
            }
        )

    total_tokens = sum(_int_or_zero(row["tokens"]) for row in rows)
    total_cost = sum(_int_or_zero(row["cost_usd_micros"]) for row in rows)
    passed = sum(1 for row in rows if row["verifier_passed"])
    cases = len(rows)
    summary = {
        "cases": cases,
        "passed": passed,
        "failed": cases - passed,
        "total_tokens": total_tokens,
        "total_cost_usd_micros": total_cost,
        "jd_metrics": {
            "success_rate": passed / cases if cases else 0.0,
            "average_tokens_per_case": total_tokens / cases if cases else 0.0,
            "average_cost_usd_micros_per_case": total_cost / cases if cases else 0.0,
            "tool_failures": sum(_int_or_zero(row["tool_failures"]) for row in rows),
            "verifier_failures": sum(1 for row in rows if row["verifier_failed"]),
            "human_interventions": sum(
                _int_or_zero(row["human_interventions"]) for row in rows
            ),
        },
        "rows": rows,
    }
    print("\nBUG_FIX_V2_EVAL_BASELINE_JSON")
    print(json.dumps(summary, indent=2, sort_keys=True))
    captured = capsys.readouterr()
    if captured.out:
        import sys

        sys.stdout.write(captured.out)

    assert len(rows) == len(_CASES)
    assert all(row["llm_calls"] > 0 for row in rows)
    assert all(row["tool_events"] > 0 for row in rows)


async def _usage_summary(
    db_pool: DatabasePool,
    tenant_id: str,
    task_id: str,
) -> dict[str, int]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT total_tokens, cost_usd_micros FROM llm_usage_logs "
            "WHERE tenant_id=$1 AND task_id=$2",
            tenant_id,
            task_id,
        )
    return {
        "llm_calls": len(rows),
        "tokens": sum(_int_or_zero(row["total_tokens"]) for row in rows),
        "cost_usd_micros": sum(_int_or_zero(row["cost_usd_micros"]) for row in rows),
    }


async def _audit_summary(
    db_pool: DatabasePool,
    tenant_id: str,
    task_id: str,
) -> dict[str, int]:
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT action FROM audit_events WHERE tenant_id=$1 AND task_id=$2",
            tenant_id,
            task_id,
        )
    tool_rows = [row for row in rows if str(row["action"]).startswith("tool.")]
    return {
        "tool_events": len(tool_rows),
        "tool_failures": sum(1 for row in tool_rows if row["action"] == "tool.failed"),
        "human_interventions": sum(
            1
            for row in rows
            if row["action"] in {"task.awaiting_approval", "permission.prompted"}
        ),
    }


def _int_or_zero(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0
