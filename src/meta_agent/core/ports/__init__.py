"""Port abstractions: business-layer contracts for external systems.

【目标】仓储、队列、LLM、Git、限流、熔断、Secret、对象存储等 Port 抽象。
【当前】持久化、消息队列、LLM Port。

Per CLAUDE.md "实现约束": ports are language- and host-neutral; concrete
adapters live under ``meta_agent.infra``. Business code (orchestration,
capabilities) only depends on ports, never on adapter modules.
"""

from meta_agent.core.ports.git_provider import (
    GitProvider,
    GitProviderAuthError,
    GitProviderError,
    GitProviderInvalidRequestError,
    GitProviderTransientError,
    PullRequestAction,
    PullRequestRef,
)
from meta_agent.core.ports.llm import (
    ChatMessage,
    FinishReason,
    LLMAuthError,
    LLMClient,
    LLMError,
    LLMInvalidRequestError,
    LLMRateLimitedError,
    LLMRequest,
    LLMResponse,
    LLMTransientError,
    LLMUsage,
    MessageRole,
)
from meta_agent.core.ports.llm_usage import LLMUsageRepository
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
from meta_agent.core.ports.workspace import WorkspaceError, WorkspaceManager

__all__ = [
    "AuditRepository",
    "ChatMessage",
    "CheckpointRepository",
    "FinishReason",
    "GitProvider",
    "GitProviderAuthError",
    "GitProviderError",
    "GitProviderInvalidRequestError",
    "GitProviderTransientError",
    "LLMAuthError",
    "LLMClient",
    "LLMError",
    "LLMInvalidRequestError",
    "LLMRateLimitedError",
    "LLMRequest",
    "LLMResponse",
    "LLMTransientError",
    "LLMUsage",
    "LLMUsageRepository",
    "MessageConsumer",
    "MessageEnvelope",
    "MessageHandler",
    "MessagePublisher",
    "MessageRole",
    "OutboxRepository",
    "PullRequestAction",
    "PullRequestRef",
    "QueueError",
    "RepositoryError",
    "SessionRepository",
    "TaskRepository",
    "TenantIsolationError",
    "WorkspaceError",
    "WorkspaceManager",
]
