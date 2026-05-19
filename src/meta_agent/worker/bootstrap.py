"""Worker process assembly.

Wires production adapters into a :class:`WorkerLoop`:

* asyncpg pool + PG repositories (task / checkpoint / audit / llm_usage)
* :class:`OpenRouterClient` wrapped in :class:`MeteredLLMClient` so every
  LLM call is accounted for in ``llm_usage_logs``
* :class:`GraphRegistry` with built-in graphs registered and materialized
* :class:`RedisStreamConsumer` exposing ``claim_batch`` / ``ack``

Environment variables are read exactly once in
:meth:`WorkerSettings.from_env`. The remainder is pure wiring so the
registry assembly can be exercised in unit tests without opening sockets.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from redis.asyncio import Redis

from meta_agent.core.domain.task import TaskType
from meta_agent.core.orchestration import (
    GraphDeps,
    GraphRegistry,
    TaskChainRegistry,
    bug_fix_to_auto_pr_policy,
)
from meta_agent.core.orchestration.graphs import (
    AUTO_PR_GRAPH_ID,
    BUG_FIX_GRAPH_ID,
    CODE_REVIEW_GRAPH_ID,
    ECHO_GRAPH_ID,
    GIT_INSPECT_GRAPH_ID,
    SIMPLE_CHAT_GRAPH_ID,
    build_auto_pr_graph,
    build_bug_fix_graph,
    build_code_review_graph,
    build_echo_graph,
    build_git_inspect_graph,
    build_simple_chat_graph,
)
from meta_agent.core.ports.git_provider import GitProvider
from meta_agent.core.ports.llm import LLMClient
from meta_agent.infra.git_provider import (
    FakeGitProvider,
    GitHubGitProvider,
    GitHubGitProviderConfig,
)
from meta_agent.infra.llm import MeteredLLMClient, OpenRouterClient, OpenRouterConfig
from meta_agent.infra.persistence import (
    PgAuditRepository,
    PgCheckpointRepository,
    PgLLMUsageRepository,
    PgOutboxRepository,
    PgTaskRepository,
    PgTaskSubmitter,
    build_pool,
)
from meta_agent.infra.persistence.pool import PoolConfig
from meta_agent.infra.queue import RedisStreamConsumer
from meta_agent.infra.workspace import LocalGitConfig, LocalGitWorkspaceManager
from meta_agent.worker.runner import WorkerConfig, WorkerLoop

_DB_URL_ENV = "META_AGENT_DB_URL"
_REDIS_URL_ENV = "META_AGENT_REDIS_URL"
_TOPIC_ENV = "META_AGENT_TASK_TOPIC"
_GROUP_ENV = "META_AGENT_WORKER_GROUP"
_NAME_ENV = "META_AGENT_WORKER_NAME"
_MAX_ATTEMPTS_ENV = "META_AGENT_WORKER_MAX_ATTEMPTS"
_BLOCK_MS_ENV = "META_AGENT_WORKER_BLOCK_MS"
_DB_MIN_SIZE_ENV = "META_AGENT_WORKER_DB_MIN_SIZE"
_DB_MAX_SIZE_ENV = "META_AGENT_WORKER_DB_MAX_SIZE"
_WORKSPACE_ROOT_ENV = "META_AGENT_WORKSPACE_ROOT"
_GIT_PROVIDER_ENV = "META_AGENT_GIT_PROVIDER"

_DEFAULT_DB_URL = "postgresql://meta_agent:dev-only@localhost:5432/meta_agent"
_DEFAULT_REDIS_URL = "redis://localhost:6379/0"
_DEFAULT_TASK_TOPIC = "task.commands"
_DEFAULT_GROUP = "workers"
_DEFAULT_LLM_PROVIDER = "openrouter"
_DEFAULT_WORKSPACE_ROOT = "/var/lib/meta-agent/workspaces"
_DEFAULT_GIT_PROVIDER = "fake"
_SUPPORTED_GIT_PROVIDERS = ("fake", "github")


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """Process-level configuration for the worker entrypoint."""

    db_url: str
    redis_url: str
    task_topic: str
    consumer_group: str
    consumer_name: str
    openrouter: OpenRouterConfig
    workspace_root: Path
    git_provider: str
    github: GitHubGitProviderConfig | None
    db_min_size: int = 1
    db_max_size: int = 5
    max_attempts: int = 3
    block_ms: int = 1_000

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> WorkerSettings:
        source: dict[str, str] = dict(env if env is not None else os.environ)
        git_provider = source.get(_GIT_PROVIDER_ENV, _DEFAULT_GIT_PROVIDER).strip().lower()
        if git_provider not in _SUPPORTED_GIT_PROVIDERS:
            raise ValueError(
                f"{_GIT_PROVIDER_ENV}={git_provider!r} not in {_SUPPORTED_GIT_PROVIDERS}"
            )
        github_cfg = GitHubGitProviderConfig.from_env(source) if git_provider == "github" else None
        return cls(
            db_url=source.get(_DB_URL_ENV, _DEFAULT_DB_URL),
            redis_url=source.get(_REDIS_URL_ENV, _DEFAULT_REDIS_URL),
            task_topic=source.get(_TOPIC_ENV, _DEFAULT_TASK_TOPIC),
            consumer_group=source.get(_GROUP_ENV, _DEFAULT_GROUP),
            consumer_name=source.get(_NAME_ENV, "") or socket.gethostname(),
            openrouter=OpenRouterConfig.from_env(source),
            workspace_root=Path(source.get(_WORKSPACE_ROOT_ENV, _DEFAULT_WORKSPACE_ROOT)),
            git_provider=git_provider,
            github=github_cfg,
            db_min_size=int(source.get(_DB_MIN_SIZE_ENV, "1")),
            db_max_size=int(source.get(_DB_MAX_SIZE_ENV, "5")),
            max_attempts=int(source.get(_MAX_ATTEMPTS_ENV, "3")),
            block_ms=int(source.get(_BLOCK_MS_ENV, "1000")),
        )


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    """Wired :class:`WorkerLoop` plus the resources it owns."""

    worker: WorkerLoop
    aclose: Callable[[], Awaitable[None]]
    resources: dict[str, object] = field(default_factory=dict)


def build_registry(deps: GraphDeps) -> GraphRegistry:
    """Register every built-in graph and materialize against ``deps``."""

    registry = GraphRegistry()
    registry.register(
        ECHO_GRAPH_ID,
        lambda _deps: build_echo_graph(),
        default_for=TaskType.SYSTEM_ECHO,
    )
    registry.register(
        SIMPLE_CHAT_GRAPH_ID,
        build_simple_chat_graph,
        default_for=TaskType.SYSTEM_CHAT,
    )
    registry.register(
        GIT_INSPECT_GRAPH_ID,
        lambda _deps: build_git_inspect_graph(),
        default_for=TaskType.SYSTEM_GIT_INSPECT,
        requires_workspace=True,
    )
    registry.register(
        BUG_FIX_GRAPH_ID,
        build_bug_fix_graph,
        default_for=TaskType.BUG_FIX,
        requires_workspace=True,
    )
    registry.register(
        CODE_REVIEW_GRAPH_ID,
        build_code_review_graph,
        default_for=TaskType.CODE_REVIEW,
    )
    registry.register(
        AUTO_PR_GRAPH_ID,
        build_auto_pr_graph,
        default_for=TaskType.AUTO_PR,
    )
    registry.materialize(deps)
    return registry


def build_metered_llm(inner: LLMClient, recorder: PgLLMUsageRepository) -> MeteredLLMClient:
    """Wrap ``inner`` with the usage-recording decorator."""

    return MeteredLLMClient(inner, recorder, provider=_DEFAULT_LLM_PROVIDER)


def build_chain_registry() -> TaskChainRegistry:
    """Register every built-in task-chain policy.

    v1 ships a single edge: a successful ``BUG_FIX`` run that pushes
    its commit triggers an ``AUTO_PR`` follow-up. The submitter and
    runner only fire the chain when both halves of the hook are
    wired, so leaving the registry empty (or omitting either side in
    a unit-test bootstrap) cleanly disables chaining.
    """

    registry = TaskChainRegistry()
    registry.register(TaskType.BUG_FIX, bug_fix_to_auto_pr_policy)
    return registry


def build_git_provider(settings: WorkerSettings) -> GitProvider:
    """Pick the git provider adapter based on ``settings.git_provider``.

    Defaults to :class:`FakeGitProvider` so smoke / dev environments
    do not require GitHub credentials. ``github`` requires the matching
    :class:`GitHubGitProviderConfig` to have been built in ``from_env``.
    """

    if settings.git_provider == "github":
        if settings.github is None:
            raise ValueError("git_provider=github requires WorkerSettings.github")
        return GitHubGitProvider(settings.github)
    return FakeGitProvider()


async def build_worker(settings: WorkerSettings) -> WorkerRuntime:
    """Open infra connections, wire :class:`WorkerLoop`, return runtime.

    Callers must invoke ``await runtime.aclose()`` exactly once during
    shutdown to release the asyncpg pool, the Redis client, the
    ``httpx`` pool inside :class:`OpenRouterClient`, and the
    ``httpx`` pool inside the git provider adapter.
    """

    pool = await build_pool(
        PoolConfig(
            dsn=settings.db_url,
            min_size=settings.db_min_size,
            max_size=settings.db_max_size,
        )
    )
    redis_client: Redis = Redis.from_url(settings.redis_url, decode_responses=False)
    consumer = RedisStreamConsumer(
        redis_client,
        topic=settings.task_topic,
        group=settings.consumer_group,
        consumer_name=settings.consumer_name,
        block_ms=settings.block_ms,
    )
    task_repo = PgTaskRepository(pool)
    checkpoint_repo = PgCheckpointRepository(pool)
    audit_repo = PgAuditRepository(pool)
    outbox_repo = PgOutboxRepository(pool)
    usage_repo = PgLLMUsageRepository(pool)
    inner_llm = OpenRouterClient(settings.openrouter)
    metered_llm = build_metered_llm(inner_llm, usage_repo)
    git_provider = build_git_provider(settings)
    # Reuse the GitHub adapter's token for ``git push`` so a single
    # secret covers both PR creation (port-mediated) and pushing local
    # commits (subprocess-mediated). With the fake provider there is no
    # remote to push to, so ``None`` makes the push node skip cleanly.
    push_token = settings.github.token if settings.github is not None else None
    registry = build_registry(
        GraphDeps(llm=metered_llm, git_provider=git_provider, git_push_token=push_token)
    )
    # Ensure the workspace root exists; the local-git adapter requires
    # the directory to be present so it can ``mkdir`` per-task subdirs.
    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    workspaces = LocalGitWorkspaceManager(LocalGitConfig(root_dir=settings.workspace_root))
    submitter = PgTaskSubmitter(pool, task_repo, outbox_repo)
    chain_registry = build_chain_registry()
    worker = WorkerLoop(
        stream=consumer,
        tasks=task_repo,
        checkpoints=checkpoint_repo,
        audits=audit_repo,
        registry=registry,
        workspaces=workspaces,
        submitter=submitter,
        chain_registry=chain_registry,
        config=WorkerConfig(max_attempts=settings.max_attempts, block_ms=settings.block_ms),
    )

    async def _aclose() -> None:
        await git_provider.close()
        await metered_llm.close()
        await redis_client.aclose()
        await pool.close()

    return WorkerRuntime(
        worker=worker,
        aclose=_aclose,
        resources={
            "pool": pool,
            "redis": redis_client,
            "llm": metered_llm,
            "git_provider": git_provider,
        },
    )
