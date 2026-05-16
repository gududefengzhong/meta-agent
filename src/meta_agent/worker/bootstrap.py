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

from redis.asyncio import Redis

from meta_agent.core.domain.task import TaskType
from meta_agent.core.orchestration import GraphDeps, GraphRegistry
from meta_agent.core.orchestration.graphs import (
    ECHO_GRAPH_ID,
    SIMPLE_CHAT_GRAPH_ID,
    build_echo_graph,
    build_simple_chat_graph,
)
from meta_agent.core.ports.llm import LLMClient
from meta_agent.infra.llm import MeteredLLMClient, OpenRouterClient, OpenRouterConfig
from meta_agent.infra.persistence import (
    PgAuditRepository,
    PgCheckpointRepository,
    PgLLMUsageRepository,
    PgTaskRepository,
    build_pool,
)
from meta_agent.infra.persistence.pool import PoolConfig
from meta_agent.infra.queue import RedisStreamConsumer
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

_DEFAULT_DB_URL = "postgresql://meta_agent:dev-only@localhost:5432/meta_agent"
_DEFAULT_REDIS_URL = "redis://localhost:6379/0"
_DEFAULT_TASK_TOPIC = "task.commands"
_DEFAULT_GROUP = "workers"
_DEFAULT_LLM_PROVIDER = "openrouter"


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    """Process-level configuration for the worker entrypoint."""

    db_url: str
    redis_url: str
    task_topic: str
    consumer_group: str
    consumer_name: str
    openrouter: OpenRouterConfig
    db_min_size: int = 1
    db_max_size: int = 5
    max_attempts: int = 3
    block_ms: int = 1_000

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> WorkerSettings:
        source: dict[str, str] = dict(env if env is not None else os.environ)
        return cls(
            db_url=source.get(_DB_URL_ENV, _DEFAULT_DB_URL),
            redis_url=source.get(_REDIS_URL_ENV, _DEFAULT_REDIS_URL),
            task_topic=source.get(_TOPIC_ENV, _DEFAULT_TASK_TOPIC),
            consumer_group=source.get(_GROUP_ENV, _DEFAULT_GROUP),
            consumer_name=source.get(_NAME_ENV, "") or socket.gethostname(),
            openrouter=OpenRouterConfig.from_env(source),
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
    registry.materialize(deps)
    return registry


def build_metered_llm(inner: LLMClient, recorder: PgLLMUsageRepository) -> MeteredLLMClient:
    """Wrap ``inner`` with the usage-recording decorator."""

    return MeteredLLMClient(inner, recorder, provider=_DEFAULT_LLM_PROVIDER)


async def build_worker(settings: WorkerSettings) -> WorkerRuntime:
    """Open infra connections, wire :class:`WorkerLoop`, return runtime.

    Callers must invoke ``await runtime.aclose()`` exactly once during
    shutdown to release the asyncpg pool, the Redis client and the
    underlying ``httpx`` connection pool inside :class:`OpenRouterClient`.
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
    usage_repo = PgLLMUsageRepository(pool)
    inner_llm = OpenRouterClient(settings.openrouter)
    metered_llm = build_metered_llm(inner_llm, usage_repo)
    registry = build_registry(GraphDeps(llm=metered_llm))
    worker = WorkerLoop(
        stream=consumer,
        tasks=task_repo,
        checkpoints=checkpoint_repo,
        audits=audit_repo,
        registry=registry,
        config=WorkerConfig(max_attempts=settings.max_attempts, block_ms=settings.block_ms),
    )

    async def _aclose() -> None:
        await metered_llm.close()
        await redis_client.aclose()
        await pool.close()

    return WorkerRuntime(
        worker=worker,
        aclose=_aclose,
        resources={"pool": pool, "redis": redis_client, "llm": metered_llm},
    )
