"""PostgreSQL adapters for the repository ports.

【当前】asyncpg 连接池 + 五个仓储骨架，租户守门基于
:func:`meta_agent.infra.security.context.require_tenant_id`。
【目标】完整查询面、读写分离、慢查询追踪、JSONB 索引等。
"""

from meta_agent.infra.persistence.audit_repo import PgAuditRepository
from meta_agent.infra.persistence.checkpoint_repo import PgCheckpointRepository
from meta_agent.infra.persistence.outbox_dispatcher import (
    DispatcherConfig,
    OutboxDispatcher,
)
from meta_agent.infra.persistence.outbox_repo import PgOutboxRepository
from meta_agent.infra.persistence.pool import DatabasePool, build_pool
from meta_agent.infra.persistence.session_repo import PgSessionRepository
from meta_agent.infra.persistence.task_repo import PgTaskRepository

__all__ = [
    "DatabasePool",
    "DispatcherConfig",
    "OutboxDispatcher",
    "PgAuditRepository",
    "PgCheckpointRepository",
    "PgOutboxRepository",
    "PgSessionRepository",
    "PgTaskRepository",
    "build_pool",
]
