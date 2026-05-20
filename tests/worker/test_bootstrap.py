"""Unit tests for the worker process bootstrap.

These tests exercise pure wiring helpers (settings parsing and
registry assembly). The real :func:`build_worker` opens Postgres /
Redis / OpenRouter connections; that path is intentionally covered by
the integration suite (``tests/integration``) and the ``docker
compose`` smoke flow rather than by mocking every adapter here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meta_agent.core.domain.task import TaskType
from meta_agent.core.orchestration import GraphDeps
from meta_agent.core.orchestration.graphs import (
    ECHO_GRAPH_ID,
    GIT_INSPECT_GRAPH_ID,
    SIMPLE_CHAT_GRAPH_ID,
)
from meta_agent.infra.budget.llm_usage_aggregator import (
    LLMUsageAggregatorBudgetEnforcer,
)
from meta_agent.infra.budget.noop import NoopBudgetEnforcer
from meta_agent.infra.circuitbreaker.in_memory import InMemoryCircuitBreaker
from meta_agent.infra.circuitbreaker.noop import NoopCircuitBreaker
from meta_agent.infra.llm.budget_enforcing import BudgetEnforcingLLMClient
from meta_agent.infra.llm.circuit_breaking import CircuitBreakingLLMClient
from meta_agent.infra.llm.rate_limited import RateLimitedLLMClient
from meta_agent.infra.ratelimit.in_memory import InMemoryTokenBucketRateLimiter
from meta_agent.infra.ratelimit.noop import NoopRateLimiter
from meta_agent.worker.bootstrap import (
    WorkerSettings,
    build_budget_enforcer,
    build_budget_enforcer_from_env,
    build_budget_enforcing_llm,
    build_chain_registry,
    build_circuit_breaker,
    build_circuit_breaker_from_env,
    build_circuit_breaking_llm,
    build_rate_limited_llm,
    build_rate_limiter,
    build_rate_limiter_from_env,
    build_registry,
)
from tests.core.orchestration._fakes import FakeLLMClient


def _env(**overrides: str) -> dict[str, str]:
    base: dict[str, str] = {
        "OPENROUTER_API_KEY": "sk-or-test-1234",
    }
    base.update(overrides)
    return base


def test_settings_from_env_uses_documented_defaults() -> None:
    settings = WorkerSettings.from_env(_env())
    assert settings.db_url.startswith("postgresql://")
    assert settings.redis_url.startswith("redis://")
    assert settings.task_topic == "task.commands"
    assert settings.consumer_group == "workers"
    assert settings.consumer_name  # hostname-derived, non-empty
    assert settings.max_attempts == 3
    assert settings.block_ms == 1_000
    assert settings.openrouter.api_key == "sk-or-test-1234"
    assert settings.workspace_root == Path("/var/lib/meta-agent/workspaces")
    # Default git provider must be ``fake`` so dev/smoke environments
    # do not require a GitHub token to start the worker.
    assert settings.git_provider == "fake"
    assert settings.github is None


def test_settings_from_env_selects_github_provider() -> None:
    settings = WorkerSettings.from_env(
        _env(
            META_AGENT_GIT_PROVIDER="github",
            META_AGENT_GITHUB_TOKEN="ghp_test_token",
            META_AGENT_GITHUB_BASE_URL="https://ghe.example.com/api/v3",
        )
    )
    assert settings.git_provider == "github"
    assert settings.github is not None
    assert settings.github.token == "ghp_test_token"
    assert settings.github.base_url == "https://ghe.example.com/api/v3"


def test_settings_from_env_github_requires_token() -> None:
    with pytest.raises(ValueError, match="META_AGENT_GITHUB_TOKEN"):
        WorkerSettings.from_env(_env(META_AGENT_GIT_PROVIDER="github"))


def test_settings_from_env_rejects_unknown_git_provider() -> None:
    with pytest.raises(ValueError, match="META_AGENT_GIT_PROVIDER"):
        WorkerSettings.from_env(_env(META_AGENT_GIT_PROVIDER="gitlab"))


def test_settings_from_env_overrides_each_knob() -> None:
    settings = WorkerSettings.from_env(
        _env(
            META_AGENT_DB_URL="postgresql://u:p@db:5432/x",
            META_AGENT_REDIS_URL="redis://r:6379/2",
            META_AGENT_TASK_TOPIC="custom.topic",
            META_AGENT_WORKER_GROUP="g-1",
            META_AGENT_WORKER_NAME="worker-7",
            META_AGENT_WORKER_MAX_ATTEMPTS="5",
            META_AGENT_WORKER_BLOCK_MS="250",
            META_AGENT_WORKER_DB_MIN_SIZE="2",
            META_AGENT_WORKER_DB_MAX_SIZE="20",
            META_AGENT_WORKSPACE_ROOT="/tmp/custom-ws",
        )
    )
    assert settings.db_url == "postgresql://u:p@db:5432/x"
    assert settings.redis_url == "redis://r:6379/2"
    assert settings.task_topic == "custom.topic"
    assert settings.consumer_group == "g-1"
    assert settings.consumer_name == "worker-7"
    assert settings.max_attempts == 5
    assert settings.block_ms == 250
    assert settings.db_min_size == 2
    assert settings.db_max_size == 20
    assert settings.workspace_root == Path("/tmp/custom-ws")


def test_settings_from_env_requires_openrouter_key() -> None:
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        WorkerSettings.from_env({})


def test_build_registry_registers_builtin_graphs_and_routes_defaults() -> None:
    registry = build_registry(GraphDeps(llm=FakeLLMClient()))
    assert registry.is_materialized
    assert registry.get(ECHO_GRAPH_ID).graph_id == ECHO_GRAPH_ID
    assert registry.get(SIMPLE_CHAT_GRAPH_ID).graph_id == SIMPLE_CHAT_GRAPH_ID
    assert registry.get(GIT_INSPECT_GRAPH_ID).graph_id == GIT_INSPECT_GRAPH_ID
    assert registry.resolve(TaskType.SYSTEM_ECHO).graph_id == ECHO_GRAPH_ID
    assert registry.resolve(TaskType.SYSTEM_CHAT).graph_id == SIMPLE_CHAT_GRAPH_ID
    assert registry.resolve(TaskType.SYSTEM_GIT_INSPECT).graph_id == GIT_INSPECT_GRAPH_ID
    # Only the git-inspect graph requires a workspace; the other two
    # built-ins must not pull the worker into provisioning a worktree.
    assert registry.requires_workspace(GIT_INSPECT_GRAPH_ID) is True
    assert registry.requires_workspace(ECHO_GRAPH_ID) is False
    assert registry.requires_workspace(SIMPLE_CHAT_GRAPH_ID) is False


def test_build_chain_registry_registers_bug_fix_to_auto_pr() -> None:
    from datetime import UTC, datetime

    from meta_agent.core.domain.task import Task, TaskState
    from meta_agent.core.orchestration.result import TaskResult

    registry = build_chain_registry()
    now = datetime(2026, 5, 15, tzinfo=UTC)
    parent = Task(
        task_id="parent-1",
        tenant_id="tenant-1",
        principal_id="user-1",
        trace_id="trace-1",
        task_type=TaskType.BUG_FIX,
        state=TaskState.SUCCEEDED,
        input_payload={"issue_description": "fix x"},
        created_at=now,
        updated_at=now,
    )
    result = TaskResult(
        task_id="parent-1",
        tenant_id="tenant-1",
        trace_id="trace-1",
        graph_id="builtin.bug_fix",
        status="succeeded",
        output={
            "repo_url": "https://example.com/repo.git",
            "head_commit_sha": "deadbeef",
            "head_branch": "agent/parent-1",
            "base_ref": "main",
            "pushed": True,
            "verifier_passed": True,
            "verifier_output": "",
            "diff_stat": "",
        },
        node_sequence=4,
        started_at=now,
        finished_at=now,
    )
    spec = registry.derive(parent, result)
    assert spec is not None and spec.task_type is TaskType.AUTO_PR


def test_build_rate_limiter_defaults_to_noop() -> None:
    limiter = build_rate_limiter()
    assert isinstance(limiter, NoopRateLimiter)


def test_build_rate_limiter_from_env_defaults_to_noop() -> None:
    limiter = build_rate_limiter_from_env({})
    assert isinstance(limiter, NoopRateLimiter)


def test_build_rate_limiter_from_env_selects_memory_backend() -> None:
    limiter = build_rate_limiter_from_env({"META_AGENT_RATELIMIT_BACKEND": "memory"})
    assert isinstance(limiter, InMemoryTokenBucketRateLimiter)


def test_build_rate_limiter_from_env_redis_requires_client() -> None:
    with pytest.raises(ValueError, match="requires a Redis client"):
        build_rate_limiter_from_env({"META_AGENT_RATELIMIT_BACKEND": "redis"})


def test_build_rate_limited_llm_wraps_inner() -> None:
    inner = FakeLLMClient()
    limiter = NoopRateLimiter()
    client = build_rate_limited_llm(inner, limiter)
    assert isinstance(client, RateLimitedLLMClient)


def test_build_rate_limited_llm_threads_audit_sink() -> None:
    from meta_agent.core.domain.audit import AuditEvent
    from meta_agent.core.ports.audit_sink import AuditSink

    class _NullSink(AuditSink):
        async def append(self, event: AuditEvent) -> None:
            return None

    sink = _NullSink()
    client = build_rate_limited_llm(FakeLLMClient(), NoopRateLimiter(), audit_sink=sink)
    assert client._audit_sink is sink


def test_build_circuit_breaker_defaults_to_noop() -> None:
    breaker = build_circuit_breaker()
    assert isinstance(breaker, NoopCircuitBreaker)


def test_build_circuit_breaker_from_env_defaults_to_noop() -> None:
    breaker = build_circuit_breaker_from_env({})
    assert isinstance(breaker, NoopCircuitBreaker)


def test_build_circuit_breaker_from_env_selects_memory_backend() -> None:
    breaker = build_circuit_breaker_from_env({"META_AGENT_CIRCUITBREAKER_BACKEND": "memory"})
    assert isinstance(breaker, InMemoryCircuitBreaker)


def test_build_circuit_breaker_from_env_redis_requires_client() -> None:
    with pytest.raises(ValueError, match="requires a Redis client"):
        build_circuit_breaker_from_env({"META_AGENT_CIRCUITBREAKER_BACKEND": "redis"})


def test_build_circuit_breaking_llm_wraps_inner() -> None:
    inner = FakeLLMClient()
    breaker = NoopCircuitBreaker()
    client = build_circuit_breaking_llm(inner, breaker)
    assert isinstance(client, CircuitBreakingLLMClient)


def test_build_circuit_breaking_llm_threads_audit_sink() -> None:
    from meta_agent.core.domain.audit import AuditEvent
    from meta_agent.core.ports.audit_sink import AuditSink

    class _NullSink(AuditSink):
        async def append(self, event: AuditEvent) -> None:
            return None

    sink = _NullSink()
    client = build_circuit_breaking_llm(FakeLLMClient(), NoopCircuitBreaker(), audit_sink=sink)
    assert client._audit_sink is sink


def test_build_budget_enforcer_defaults_to_noop() -> None:
    enforcer = build_budget_enforcer()
    assert isinstance(enforcer, NoopBudgetEnforcer)


def test_build_budget_enforcer_from_env_defaults_to_noop() -> None:
    enforcer, config = build_budget_enforcer_from_env({})
    assert isinstance(enforcer, NoopBudgetEnforcer)
    assert config.backend == "noop"
    assert config.cache_ttl_s == 10.0
    assert config.fail_open is True


def test_build_budget_enforcer_from_env_selects_llm_usage_backend() -> None:
    from datetime import datetime

    from meta_agent.core.domain.llm_usage import LLMUsageRecord
    from meta_agent.core.ports.budget import BudgetUsage
    from meta_agent.core.ports.llm_usage import LLMUsageRepository

    class _StubRepo(LLMUsageRepository):
        async def record(self, record: LLMUsageRecord) -> None:
            raise AssertionError

        async def list_for_task(self, tenant_id: str, task_id: str) -> list[LLMUsageRecord]:
            raise AssertionError

        async def aggregate_since(self, tenant_id: str, since: datetime) -> BudgetUsage:
            return BudgetUsage(tokens_used=0, cost_usd_micros_used=0)

    enforcer, config = build_budget_enforcer_from_env(
        {
            "META_AGENT_BUDGET_BACKEND": "llm_usage",
            "META_AGENT_BUDGET_MAX_TOKENS": "100000",
            "META_AGENT_BUDGET_CACHE_TTL_S": "5",
            "META_AGENT_BUDGET_FAIL_OPEN": "false",
        },
        usage_repo=_StubRepo(),
    )
    assert isinstance(enforcer, LLMUsageAggregatorBudgetEnforcer)
    assert config.max_tokens_per_month == 100_000
    assert config.cache_ttl_s == 5.0
    assert config.fail_open is False


def test_build_budget_enforcer_from_env_llm_usage_requires_repo() -> None:
    with pytest.raises(ValueError, match="requires an LLMUsageRepository"):
        build_budget_enforcer_from_env({"META_AGENT_BUDGET_BACKEND": "llm_usage"})


def test_build_budget_enforcing_llm_wraps_inner() -> None:
    client = build_budget_enforcing_llm(FakeLLMClient(), NoopBudgetEnforcer())
    assert isinstance(client, BudgetEnforcingLLMClient)


def test_build_budget_enforcing_llm_threads_audit_sink_and_knobs() -> None:
    from meta_agent.core.domain.audit import AuditEvent
    from meta_agent.core.ports.audit_sink import AuditSink

    class _NullSink(AuditSink):
        async def append(self, event: AuditEvent) -> None:
            return None

    sink = _NullSink()
    client = build_budget_enforcing_llm(
        FakeLLMClient(),
        NoopBudgetEnforcer(),
        cache_ttl_s=3.0,
        fail_open=False,
        audit_sink=sink,
    )
    assert client._audit_sink is sink
    assert client._cache_ttl_s == 3.0
    assert client._fail_open is False
