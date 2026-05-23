"""PostgreSQL adapters for the repository ports.

【当前】asyncpg 连接池 + 五个仓储骨架，租户守门基于
:func:`meta_agent.infra.security.context.require_tenant_id`。
【目标】完整查询面、读写分离、慢查询追踪、JSONB 索引等。
"""

from meta_agent.infra.persistence.audit_repo import PgAuditRepository
from meta_agent.infra.persistence.checkpoint_repo import PgCheckpointRepository
from meta_agent.infra.persistence.llm_usage_repo import PgLLMUsageRepository
from meta_agent.infra.persistence.outbox_dispatcher import (
    DispatcherConfig,
    OutboxDispatcher,
)
from meta_agent.infra.persistence.outbox_repo import PgOutboxRepository
from meta_agent.infra.persistence.pool import DatabasePool, build_pool
from meta_agent.infra.persistence.session_repo import PgSessionRepository
from meta_agent.infra.persistence.task_repo import PgTaskRepository
from meta_agent.infra.persistence.task_submitter import PgTaskSubmitter
from meta_agent.infra.persistence.trajectory_repo import PgTrajectoryRepository
from meta_agent.infra.persistence.webhook_repo import (
    PgWebhookDeliveryRepository,
    PgWebhookSubscriptionRepository,
)

__all__ = [
    "DatabasePool",
    "DispatcherConfig",
    "OutboxDispatcher",
    "PgAuditRepository",
    "PgCheckpointRepository",
    "PgLLMUsageRepository",
    "PgOutboxRepository",
    "PgSessionRepository",
    "PgTaskRepository",
    "PgTaskSubmitter",
    "PgTrajectoryRepository",
    "PgWebhookDeliveryRepository",
    "PgWebhookSubscriptionRepository",
    "build_pool",
]
