"""Worker process assembly.

Wires production adapters into a :class:`WorkerLoop`:

* asyncpg pool + PG repositories (task / checkpoint / audit / llm_usage)
* :class:`OpenRouterClient` wrapped (innermost-out) in
  :class:`CircuitBreakingLLMClient` (guards provider failures only),
  :class:`MeteredLLMClient` (writes ``llm_usage_logs``),
  :class:`RateLimitedLLMClient` (intercepts denied calls *before*
  metering), and :class:`BudgetEnforcingLLMClient` (rejects calls when
  the tenant's monthly token cap is exhausted, *before* any rate-limit
  token is consumed). The rate-limiter, breaker, and budget backends
  are selected by ``META_AGENT_RATELIMIT_BACKEND``,
  ``META_AGENT_CIRCUITBREAKER_BACKEND``, and
  ``META_AGENT_BUDGET_BACKEND`` respectively; all default to NoOp so
  unconfigured deployments keep their previous behaviour.
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

from meta_agent.core.capabilities import ToolExecutor, ToolRegistry
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
    BUG_FIX_V2_GRAPH_ID,
    CODE_REVIEW_GRAPH_ID,
    ECHO_GRAPH_ID,
    FEATURE_IMPL_GRAPH_ID,
    GIT_INSPECT_GRAPH_ID,
    SHELL_AGENT_GRAPH_ID,
    SIMPLE_CHAT_GRAPH_ID,
    build_auto_pr_graph,
    build_bug_fix_graph,
    build_bug_fix_v2_graph,
    build_code_review_graph,
    build_echo_graph,
    build_feature_impl_graph,
    build_git_inspect_graph,
    build_shell_agent_graph,
    build_simple_chat_graph,
)
from meta_agent.core.ports.audit_sink import AuditSink
from meta_agent.core.ports.budget import BudgetEnforcer
from meta_agent.core.ports.circuit_breaker import CircuitBreaker
from meta_agent.core.ports.git_provider import GitProvider
from meta_agent.core.ports.llm import LLMClient
from meta_agent.core.ports.llm_usage import LLMUsageRepository
from meta_agent.core.ports.rate_limiter import RateLimiter
from meta_agent.core.ports.tools import DocSearchTool, WebFetchTool
from meta_agent.infra.budget import (
    BudgetConfig,
    NoopBudgetEnforcer,
    build_budget_enforcer_from_config,
)
from meta_agent.infra.circuitbreaker import (
    CircuitBreakerConfig,
    NoopCircuitBreaker,
    build_circuit_breaker_from_config,
)
from meta_agent.infra.git_provider import (
    CircuitBreakingGitProvider,
    FakeGitProvider,
    GitHubGitProvider,
    GitHubGitProviderConfig,
    RateLimitedGitProvider,
)
from meta_agent.infra.llm import (
    BudgetEnforcingLLMClient,
    CircuitBreakingLLMClient,
    MeteredLLMClient,
    OpenRouterClient,
    OpenRouterConfig,
    RateLimitedLLMClient,
)
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
from meta_agent.infra.prompt_registry import (
    CachingPromptRegistry,
    PgPromptRegistry,
    ensure_seeded,
)
from meta_agent.infra.queue import RedisStreamConsumer
from meta_agent.infra.ratelimit import (
    NoopRateLimiter,
    RateLimitConfig,
    build_rate_limiter_from_config,
)
from meta_agent.infra.secrets import build_secrets_from_env, resolve_secret_env
from meta_agent.infra.tools import (
    DockerWorkspaceEditTool,
    DockerWorkspaceFileSystemTool,
    DockerWorkspaceShellTool,
    DockerWorkspaceTestTool,
    HttpxWebFetchTool,
    LocalWorkspaceEditTool,
    LocalWorkspaceFileSystemTool,
    LocalWorkspaceShellTool,
    LocalWorkspaceTestTool,
    register_local_workspace_tools,
)
from meta_agent.infra.workspace import (
    DockerWorkspaceConfig,
    DockerWorkspaceManager,
    LocalGitConfig,
    LocalGitWorkspaceManager,
)
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
_WORKSPACE_BACKEND_ENV = "META_AGENT_WORKSPACE_BACKEND"
_WORKSPACE_DOCKER_IMAGE_ENV = "META_AGENT_WORKSPACE_DOCKER_IMAGE"
_WORKSPACE_DOCKER_NETWORK_ENV = "META_AGENT_WORKSPACE_DOCKER_NETWORK"
_GIT_PROVIDER_ENV = "META_AGENT_GIT_PROVIDER"
_WEB_ALLOWED_HOSTS_ENV = "META_AGENT_WEB_ALLOWED_HOSTS"

_DEFAULT_DB_URL = "postgresql://meta_agent:dev-only@localhost:5432/meta_agent"
_DEFAULT_REDIS_URL = "redis://localhost:6379/0"
_DEFAULT_TASK_TOPIC = "task.commands"
_DEFAULT_GROUP = "workers"
_DEFAULT_LLM_PROVIDER = "openrouter"
_DEFAULT_WORKSPACE_ROOT = "/var/lib/meta-agent/workspaces"
_DEFAULT_WORKSPACE_BACKEND = "local_git"
_DEFAULT_WORKSPACE_DOCKER_IMAGE = "meta-agent:local"
_DEFAULT_GIT_PROVIDER = "fake"
_SUPPORTED_WORKSPACE_BACKENDS = ("local_git", "docker")
_SUPPORTED_GIT_PROVIDERS = ("fake", "github")


def _parse_web_allowed_hosts(raw: str | None) -> tuple[str, ...]:
    """Parse ``META_AGENT_WEB_ALLOWED_HOSTS`` (comma-separated) into a tuple.

    An empty / unset value yields ``()`` — the worker then leaves the
    ``WebFetchTool`` unregistered and the agent never sees it.
    """

    if not raw:
        return ()
    return tuple(host.strip().lower() for host in raw.split(",") if host.strip())


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
    workspace_backend: str
    workspace_docker_image: str
    workspace_docker_network: str | None
    git_provider: str
    github: GitHubGitProviderConfig | None
    web_allowed_hosts: tuple[str, ...] = ()
    db_min_size: int = 1
    db_max_size: int = 5
    max_attempts: int = 3
    block_ms: int = 1_000

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> WorkerSettings:
        """Read settings from an env-style mapping.

        Pure / synchronous so unit tests can call without an event loop.
        Production callers should go through
        :func:`build_worker_settings_from_env` instead so the configured
        :class:`Secrets` backend gets a chance to fold credentials in.
        """
        source: dict[str, str] = dict(env if env is not None else os.environ)
        workspace_backend = (
            source.get(_WORKSPACE_BACKEND_ENV, _DEFAULT_WORKSPACE_BACKEND).strip().lower()
        )
        if workspace_backend not in _SUPPORTED_WORKSPACE_BACKENDS:
            raise ValueError(
                f"{_WORKSPACE_BACKEND_ENV}={workspace_backend!r} not in "
                f"{_SUPPORTED_WORKSPACE_BACKENDS}"
            )
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
            workspace_backend=workspace_backend,
            workspace_docker_image=source.get(
                _WORKSPACE_DOCKER_IMAGE_ENV, _DEFAULT_WORKSPACE_DOCKER_IMAGE
            ),
            workspace_docker_network=source.get(_WORKSPACE_DOCKER_NETWORK_ENV) or None,
            git_provider=git_provider,
            github=github_cfg,
            web_allowed_hosts=_parse_web_allowed_hosts(source.get(_WEB_ALLOWED_HOSTS_ENV)),
            db_min_size=int(source.get(_DB_MIN_SIZE_ENV, "1")),
            db_max_size=int(source.get(_DB_MAX_SIZE_ENV, "5")),
            max_attempts=int(source.get(_MAX_ATTEMPTS_ENV, "3")),
            block_ms=int(source.get(_BLOCK_MS_ENV, "1000")),
        )


async def build_worker_settings_from_env(
    env: dict[str, str] | None = None,
) -> WorkerSettings:
    """Resolve secrets, then parse :class:`WorkerSettings`.

    Folds the configured :class:`Secrets` backend's values into a copy
    of ``env`` (or :data:`os.environ`) before delegating to the
    synchronous :meth:`WorkerSettings.from_env`. This is the production
    entrypoint: it lets a file-based secrets backend supply
    ``OPENROUTER_API_KEY`` / ``META_AGENT_GITHUB_TOKEN`` without those
    values being exported as process env vars, while preserving the
    existing ``env`` backend as a zero-behaviour-change default.
    """
    base = dict(env if env is not None else os.environ)
    secrets = build_secrets_from_env(base)
    resolved = await resolve_secret_env(secrets, env=base)
    return WorkerSettings.from_env(resolved)


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    """Wired :class:`WorkerLoop` plus the resources it owns."""

    worker: WorkerLoop
    aclose: Callable[[], Awaitable[None]]
    resources: dict[str, object] = field(default_factory=dict)


def build_shell_tool(
    settings: WorkerSettings,
) -> LocalWorkspaceShellTool | DockerWorkspaceShellTool:
    """Materialize the shell tool for the configured workspace backend."""

    if settings.workspace_backend == "local_git":
        return LocalWorkspaceShellTool()
    if settings.workspace_backend == "docker":
        return DockerWorkspaceShellTool(workspace_root=settings.workspace_root)
    raise ValueError(
        f"unsupported workspace backend {settings.workspace_backend!r}; "
        f"expected one of {_SUPPORTED_WORKSPACE_BACKENDS}"
    )


def build_file_system_tool(
    settings: WorkerSettings,
) -> LocalWorkspaceFileSystemTool | DockerWorkspaceFileSystemTool:
    if settings.workspace_backend == "local_git":
        return LocalWorkspaceFileSystemTool()
    if settings.workspace_backend == "docker":
        return DockerWorkspaceFileSystemTool(workspace_root=settings.workspace_root)
    raise ValueError(
        f"unsupported workspace backend {settings.workspace_backend!r}; "
        f"expected one of {_SUPPORTED_WORKSPACE_BACKENDS}"
    )


def build_edit_tool(settings: WorkerSettings) -> LocalWorkspaceEditTool | DockerWorkspaceEditTool:
    if settings.workspace_backend == "local_git":
        return LocalWorkspaceEditTool()
    if settings.workspace_backend == "docker":
        return DockerWorkspaceEditTool(workspace_root=settings.workspace_root)
    raise ValueError(
        f"unsupported workspace backend {settings.workspace_backend!r}; "
        f"expected one of {_SUPPORTED_WORKSPACE_BACKENDS}"
    )


def build_test_tool(settings: WorkerSettings) -> LocalWorkspaceTestTool | DockerWorkspaceTestTool:
    if settings.workspace_backend == "local_git":
        return LocalWorkspaceTestTool()
    if settings.workspace_backend == "docker":
        return DockerWorkspaceTestTool(workspace_root=settings.workspace_root)
    raise ValueError(
        f"unsupported workspace backend {settings.workspace_backend!r}; "
        f"expected one of {_SUPPORTED_WORKSPACE_BACKENDS}"
    )


def build_web_fetch_tool(settings: WorkerSettings) -> HttpxWebFetchTool | None:
    """Instantiate :class:`HttpxWebFetchTool` when an allow-list is set.

    Returns ``None`` when ``META_AGENT_WEB_ALLOWED_HOSTS`` is empty so
    the worker bootstrap leaves the ``web_fetch`` tool unregistered —
    the agent loop simply does not see it and cannot reach outbound
    HTTP. This is the conservative default; operators must opt in to
    web fetch explicitly.
    """

    if not settings.web_allowed_hosts:
        return None
    return HttpxWebFetchTool(allowed_hosts=frozenset(settings.web_allowed_hosts))


def build_local_tool_stack(
    fs: LocalWorkspaceFileSystemTool | DockerWorkspaceFileSystemTool | None = None,
    edit: LocalWorkspaceEditTool | DockerWorkspaceEditTool | None = None,
    shell: LocalWorkspaceShellTool | DockerWorkspaceShellTool | None = None,
    test: LocalWorkspaceTestTool | DockerWorkspaceTestTool | None = None,
    web_fetch: WebFetchTool | None = None,
    doc_search: DocSearchTool | None = None,
) -> tuple[ToolRegistry, ToolExecutor]:
    """Materialize the default local-workspace tool stack.

    Constructs a fresh :class:`ToolRegistry`, registers the
    FS / Edit / Shell / Test handlers from
    :mod:`meta_agent.infra.tools`, and binds a
    :class:`ToolExecutor` to it. Returned pair is ready to attach to
    :class:`GraphDeps` for ``shell_agent``-style tool-use graphs.

    The WEB-category tools (``web_fetch`` / ``doc_search``) are
    optional. Callers that pass ``None`` for both keep the legacy
    Phase β tool surface; callers that wire them get the Phase β+ web
    surface registered alongside the existing FS / Edit / Shell / Test
    handlers.
    """

    registry = ToolRegistry()
    register_local_workspace_tools(
        registry,
        fs=fs or LocalWorkspaceFileSystemTool(),
        edit=edit or LocalWorkspaceEditTool(),
        shell=shell or LocalWorkspaceShellTool(),
        test=test or LocalWorkspaceTestTool(),
        web_fetch=web_fetch,
        doc_search=doc_search,
    )
    return registry, ToolExecutor(registry)


def build_workspace_manager(
    settings: WorkerSettings,
) -> LocalGitWorkspaceManager | DockerWorkspaceManager:
    """Materialize the configured workspace backend.

    Phase β currently supports two backends:

    * ``local_git``: host-side ``git worktree`` only
    * ``docker``: host-side ``git worktree`` plus a managed companion
      Docker container per workspace

    The Docker backend keeps ``Workspace.worktree_path`` as a host path
    for graph state and git operations, while the Docker-backed tool
    adapters execute inside the companion container against the same
    bind-mounted workspace.
    """

    settings.workspace_root.mkdir(parents=True, exist_ok=True)
    local_git = LocalGitConfig(root_dir=settings.workspace_root)
    if settings.workspace_backend == "local_git":
        return LocalGitWorkspaceManager(local_git)
    if settings.workspace_backend == "docker":
        return DockerWorkspaceManager(
            DockerWorkspaceConfig(
                local_git=local_git,
                image=settings.workspace_docker_image,
                network=settings.workspace_docker_network,
            )
        )
    raise ValueError(
        f"unsupported workspace backend {settings.workspace_backend!r}; "
        f"expected one of {_SUPPORTED_WORKSPACE_BACKENDS}"
    )


def build_registry(deps: GraphDeps) -> GraphRegistry:
    """Register every built-in graph and materialize against ``deps``.

    ``shell_agent`` is registered only when ``deps`` carries both
    ``tool_registry`` and ``tool_executor``; callers without a tool
    stack (legacy unit tests, smoke harnesses) still get a working
    registry with the L0/L1 graphs.
    """

    registry = GraphRegistry()
    has_tool_caps = deps.tool_registry is not None and deps.tool_executor is not None
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
        default_for=None if has_tool_caps else TaskType.BUG_FIX,
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
    if has_tool_caps:
        registry.register(
            BUG_FIX_V2_GRAPH_ID,
            build_bug_fix_v2_graph,
            default_for=TaskType.BUG_FIX,
            requires_workspace=True,
        )
        registry.register(
            SHELL_AGENT_GRAPH_ID,
            build_shell_agent_graph,
            default_for=TaskType.SYSTEM_SHELL_AGENT,
            requires_workspace=True,
        )
        registry.register(
            FEATURE_IMPL_GRAPH_ID,
            build_feature_impl_graph,
            default_for=TaskType.FEATURE_IMPL,
            requires_workspace=True,
        )
    registry.materialize(deps)
    return registry


def build_metered_llm(inner: LLMClient, recorder: PgLLMUsageRepository) -> MeteredLLMClient:
    """Wrap ``inner`` with the usage-recording decorator."""

    return MeteredLLMClient(inner, recorder, provider=_DEFAULT_LLM_PROVIDER)


def build_rate_limiter() -> RateLimiter:
    """Build the default rate-limiter backend (NoOp).

    Used by unit tests and any caller that wants a zero-impact limiter.
    Production wiring goes through :func:`build_rate_limiter_from_env`
    so the backend can be switched via ``META_AGENT_RATELIMIT_BACKEND``.
    """

    return NoopRateLimiter()


def build_rate_limiter_from_env(
    env: dict[str, str] | None = None,
    *,
    redis_client: Redis | None = None,
) -> RateLimiter:
    """Pick the rate-limiter backend by ``META_AGENT_RATELIMIT_*`` env.

    ``backend=redis`` requires ``redis_client`` to be passed in so the
    limiter reuses the same connection pool as the message-queue
    adapters; the env-driven selector itself never opens sockets.
    """

    config = RateLimitConfig.from_env(env)
    return build_rate_limiter_from_config(config, redis_client=redis_client)


def build_rate_limited_llm(
    inner: LLMClient,
    limiter: RateLimiter,
    *,
    audit_sink: AuditSink | None = None,
) -> RateLimitedLLMClient:
    """Wrap a (typically metered) ``inner`` with the rate-limit decorator.

    Passing ``audit_sink`` enables best-effort ``llm.rate_limited.denied``
    audit emission; unit tests can omit it.
    """

    return RateLimitedLLMClient(
        inner,
        limiter,
        provider=_DEFAULT_LLM_PROVIDER,
        audit_sink=audit_sink,
    )


def build_circuit_breaker() -> CircuitBreaker:
    """Build the default LLM-provider circuit breaker (NoOp).

    Used by unit tests and any caller that wants a zero-impact breaker.
    Production wiring goes through :func:`build_circuit_breaker_from_env`
    so the backend can be switched via
    ``META_AGENT_CIRCUITBREAKER_BACKEND``.
    """

    return NoopCircuitBreaker()


def build_circuit_breaker_from_env(
    env: dict[str, str] | None = None,
    *,
    redis_client: Redis | None = None,
) -> CircuitBreaker:
    """Pick the breaker backend by ``META_AGENT_CIRCUITBREAKER_*`` env.

    ``backend=redis`` requires ``redis_client`` to be passed in so the
    breaker reuses the same connection pool as the message-queue and
    rate-limiter adapters; the env-driven selector itself never opens
    sockets.
    """

    config = CircuitBreakerConfig.from_env(env)
    return build_circuit_breaker_from_config(config, redis_client=redis_client)


def build_circuit_breaking_llm(
    inner: LLMClient,
    breaker: CircuitBreaker,
    *,
    audit_sink: AuditSink | None = None,
) -> CircuitBreakingLLMClient:
    """Wrap a raw provider ``inner`` with the circuit-breaker decorator.

    Passing ``audit_sink`` enables best-effort
    ``llm.circuit_breaker.open`` audit emission; unit tests can omit
    it.
    """

    return CircuitBreakingLLMClient(
        inner,
        breaker,
        provider=_DEFAULT_LLM_PROVIDER,
        audit_sink=audit_sink,
    )


def build_budget_enforcer() -> BudgetEnforcer:
    """Build the default budget enforcer (NoOp).

    Used by unit tests and any caller that wants zero-impact budget
    enforcement. Production wiring goes through
    :func:`build_budget_enforcer_from_env` so the backend can be switched
    via ``META_AGENT_BUDGET_BACKEND``.
    """

    return NoopBudgetEnforcer()


def build_budget_enforcer_from_env(
    env: dict[str, str] | None = None,
    *,
    usage_repo: LLMUsageRepository | None = None,
) -> tuple[BudgetEnforcer, BudgetConfig]:
    """Pick the budget backend by ``META_AGENT_BUDGET_*`` env.

    ``backend=llm_usage`` requires ``usage_repo`` to be passed in so the
    enforcer reads the same table that :class:`MeteredLLMClient` writes;
    the env-driven selector itself never opens sockets. Returns both the
    enforcer and the parsed :class:`BudgetConfig` so callers can read
    decorator-side knobs (``cache_ttl_s``, ``fail_open``) without parsing
    env a second time.
    """

    config = BudgetConfig.from_env(env)
    enforcer = build_budget_enforcer_from_config(config, usage_repo=usage_repo)
    return enforcer, config


def build_budget_enforcing_llm(
    inner: LLMClient,
    enforcer: BudgetEnforcer,
    *,
    cache_ttl_s: float = 10.0,
    fail_open: bool = True,
    audit_sink: AuditSink | None = None,
) -> BudgetEnforcingLLMClient:
    """Wrap a (typically rate-limited) ``inner`` with the budget decorator.

    Passing ``audit_sink`` enables best-effort ``llm.budget.exceeded``
    audit emission; unit tests can omit it.
    """

    return BudgetEnforcingLLMClient(
        inner,
        enforcer,
        provider=_DEFAULT_LLM_PROVIDER,
        cache_ttl_s=cache_ttl_s,
        fail_open=fail_open,
        audit_sink=audit_sink,
    )


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


def build_circuit_breaking_git_provider(
    inner: GitProvider,
    breaker: CircuitBreaker,
    *,
    provider: str,
    audit_sink: AuditSink | None = None,
) -> CircuitBreakingGitProvider:
    """Wrap a raw ``inner`` git provider with the circuit-breaker decorator.

    Passing ``audit_sink`` enables best-effort
    ``git.circuit_breaker.open`` audit emission; unit tests can omit it.
    The ``provider`` label is embedded into the breaker key and into
    audit payloads so operators can disambiguate ``github`` from
    ``fake`` / future backends.
    """

    return CircuitBreakingGitProvider(
        inner,
        breaker,
        provider=provider,
        audit_sink=audit_sink,
    )


def build_rate_limited_git_provider(
    inner: GitProvider,
    limiter: RateLimiter,
    *,
    provider: str,
    audit_sink: AuditSink | None = None,
) -> RateLimitedGitProvider:
    """Wrap a (typically circuit-breaking) ``inner`` with the rate-limit decorator.

    Passing ``audit_sink`` enables best-effort
    ``git.rate_limited.denied`` audit emission; unit tests can omit it.
    """

    return RateLimitedGitProvider(
        inner,
        limiter,
        provider=provider,
        audit_sink=audit_sink,
    )


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
    breaker = build_circuit_breaker_from_env(redis_client=redis_client)
    breaking_llm = build_circuit_breaking_llm(inner_llm, breaker, audit_sink=audit_repo)
    metered_llm = build_metered_llm(breaking_llm, usage_repo)
    rate_limiter = build_rate_limiter_from_env(redis_client=redis_client)
    rate_limited_llm = build_rate_limited_llm(metered_llm, rate_limiter, audit_sink=audit_repo)
    budget_enforcer, budget_config = build_budget_enforcer_from_env(usage_repo=usage_repo)
    budget_enforcing_llm = build_budget_enforcing_llm(
        rate_limited_llm,
        budget_enforcer,
        cache_ttl_s=budget_config.cache_ttl_s,
        fail_open=budget_config.fail_open,
        audit_sink=audit_repo,
    )
    raw_git_provider = build_git_provider(settings)
    # Mirror the LLM safety stack: breaker innermost (counts real
    # upstream failures only), limiter outermost (deny does not reach
    # the breaker). Both layers reuse the limiter/breaker instances
    # already built for the LLM stack; per-resource isolation is
    # achieved via the ``git:{provider}:tenant=...:repo=...`` key
    # namespace, not via a separate backend.
    breaking_git_provider = build_circuit_breaking_git_provider(
        raw_git_provider,
        breaker,
        provider=settings.git_provider,
        audit_sink=audit_repo,
    )
    git_provider: GitProvider = build_rate_limited_git_provider(
        breaking_git_provider,
        rate_limiter,
        provider=settings.git_provider,
        audit_sink=audit_repo,
    )
    # Reuse the GitHub adapter's token for ``git push`` so a single
    # secret covers both PR creation (port-mediated) and pushing local
    # commits (subprocess-mediated). With the fake provider there is no
    # remote to push to, so ``None`` makes the push node skip cleanly.
    push_token = settings.github.token if settings.github is not None else None
    # Local-workspace tool stack feeds the ``shell_agent`` tool-use loop.
    # Containment for FS/Edit lives in the adapters; the executor binds
    # the same registry instance so registry mutation post-boot is the
    # only race worth defending against. ``web_fetch`` only registers
    # when ``META_AGENT_WEB_ALLOWED_HOSTS`` is configured — without an
    # allow-list the agent never gets outbound HTTP. The Phase β+
    # ``doc_search`` adapter is operator-supplied (no canned corpus),
    # so it stays unregistered in this path.
    web_fetch_tool = build_web_fetch_tool(settings)
    tool_registry, tool_executor = build_local_tool_stack(
        fs=build_file_system_tool(settings),
        edit=build_edit_tool(settings),
        shell=build_shell_tool(settings),
        test=build_test_tool(settings),
        web_fetch=web_fetch_tool,
    )
    # Prompt registry: Postgres-backed source of truth (shared across
    # workers) wrapped in a TTL cache so per-request fetches do not
    # round-trip the DB. Seed reconciliation runs on every boot — it is
    # idempotent and inserts ``version=N+1`` whenever a seed's content
    # hash drifts from the latest registered version.
    prompt_registry = CachingPromptRegistry(PgPromptRegistry(pool))
    await ensure_seeded(prompt_registry)
    registry = build_registry(
        GraphDeps(
            llm=budget_enforcing_llm,
            git_provider=git_provider,
            git_push_token=push_token,
            tool_registry=tool_registry,
            tool_executor=tool_executor,
            prompt_registry=prompt_registry,
        )
    )
    workspaces = build_workspace_manager(settings)
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
        await budget_enforcing_llm.close()
        await budget_enforcer.close()
        await rate_limiter.close()
        await breaker.close()
        if web_fetch_tool is not None:
            await web_fetch_tool.close()
        await redis_client.aclose()
        await pool.close()

    return WorkerRuntime(
        worker=worker,
        aclose=_aclose,
        resources={
            "pool": pool,
            "redis": redis_client,
            "llm": budget_enforcing_llm,
            "rate_limiter": rate_limiter,
            "circuit_breaker": breaker,
            "budget_enforcer": budget_enforcer,
            "git_provider": git_provider,
        },
    )
