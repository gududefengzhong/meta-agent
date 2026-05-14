"""Port abstractions: business-layer contracts for external systems.

【目标】仓储、队列、LLM、Git、限流、熔断、Secret、对象存储等 Port 抽象。
【当前】仅持久化与消息队列 Port。

Per CLAUDE.md "实现约束": ports are language- and host-neutral; concrete
adapters live under ``meta_agent.infra``. Business code (orchestration,
capabilities) only depends on ports, never on adapter modules.
"""

from meta_agent.core.ports.message import MessageEnvelope, MessageHandler
from meta_agent.core.ports.queue import (
    MessageConsumer,
    MessagePublisher,
    QueueError,
)
from meta_agent.core.ports.repository import (
    AuditRepository,
    CheckpointRepository,
    OutboxRepository,
    RepositoryError,
    SessionRepository,
    TaskRepository,
    TenantIsolationError,
)

__all__ = [
    "AuditRepository",
    "CheckpointRepository",
    "MessageConsumer",
    "MessageEnvelope",
    "MessageHandler",
    "MessagePublisher",
    "OutboxRepository",
    "QueueError",
    "RepositoryError",
    "SessionRepository",
    "TaskRepository",
    "TenantIsolationError",
]
