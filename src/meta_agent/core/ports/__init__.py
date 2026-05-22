"""Port abstractions: business-layer contracts for external systems.

【目标】仓储、队列、LLM、Git、限流、熔断、Secret、对象存储等 Port 抽象。
【当前】持久化、消息队列、LLM、Git、限流、熔断 Port。

Per CLAUDE.md "实现约束": ports are language- and host-neutral; concrete
adapters live under ``meta_agent.infra``. Business code (orchestration,
capabilities) only depends on ports, never on adapter modules.
"""

from meta_agent.core.ports.audit_sink import AuditSink
from meta_agent.core.ports.auth import AuthBackendError, Principal, TokenValidator
from meta_agent.core.ports.budget import (
    BudgetBackendError,
    BudgetDecision,
    BudgetEnforcer,
    BudgetUsage,
)
from meta_agent.core.ports.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerBackendError,
    CircuitBreakerOpenError,
    CircuitBreakerState,
)
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
    LLMBudgetExceededError,
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
from meta_agent.core.ports.llm_usage import (
    LLMUsageFilter,
    LLMUsageRepository,
    UsageAggregate,
    UsageGroupBy,
)
from meta_agent.core.ports.message import MessageEnvelope, MessageHandler
from meta_agent.core.ports.prompt_registry import PromptNotFoundError, PromptRegistry
from meta_agent.core.ports.queue import (
    MessageConsumer,
    MessagePublisher,
    QueueError,
)
from meta_agent.core.ports.rate_limiter import (
    RateLimitDecision,
    RateLimiter,
    RateLimiterBackendError,
)
from meta_agent.core.ports.repository import (
    AuditFilter,
    AuditRepository,
    CheckpointRepository,
    OutboxRepository,
    RepositoryError,
    SessionRepository,
    TaskRepository,
    TenantIsolationError,
)
from meta_agent.core.ports.secrets import (
    KNOWN_SECRET_KEYS,
    SECRET_KEY_GITHUB_TOKEN,
    SECRET_KEY_OPENROUTER_API_KEY,
    SecretBackendError,
    SecretNotFoundError,
    Secrets,
)
from meta_agent.core.ports.task_submitter import FollowUpSpec, TaskSubmitter
from meta_agent.core.ports.tools import (
    EditOutcome,
    EditTool,
    FileSystemTool,
    GrepHit,
    ShellOutcome,
    ShellTool,
    TestOutcome,
    TestTool,
    ToolCall,
    ToolCategory,
    ToolContext,
    ToolError,
    ToolExecutionError,
    ToolNotFoundError,
    ToolPermissionError,
    ToolResult,
    ToolSpec,
    ToolValidationError,
)
from meta_agent.core.ports.workspace import WorkspaceError, WorkspaceManager

__all__ = [
    "KNOWN_SECRET_KEYS",
    "SECRET_KEY_GITHUB_TOKEN",
    "SECRET_KEY_OPENROUTER_API_KEY",
    "AuditFilter",
    "AuditRepository",
    "AuditSink",
    "AuthBackendError",
    "BudgetBackendError",
    "BudgetDecision",
    "BudgetEnforcer",
    "BudgetUsage",
    "ChatMessage",
    "CheckpointRepository",
    "CircuitBreaker",
    "CircuitBreakerBackendError",
    "CircuitBreakerOpenError",
    "CircuitBreakerState",
    "EditOutcome",
    "EditTool",
    "FileSystemTool",
    "FinishReason",
    "FollowUpSpec",
    "GitProvider",
    "GitProviderAuthError",
    "GitProviderError",
    "GitProviderInvalidRequestError",
    "GitProviderTransientError",
    "GrepHit",
    "LLMAuthError",
    "LLMBudgetExceededError",
    "LLMClient",
    "LLMError",
    "LLMInvalidRequestError",
    "LLMRateLimitedError",
    "LLMRequest",
    "LLMResponse",
    "LLMTransientError",
    "LLMUsage",
    "LLMUsageFilter",
    "LLMUsageRepository",
    "MessageConsumer",
    "MessageEnvelope",
    "MessageHandler",
    "MessagePublisher",
    "MessageRole",
    "OutboxRepository",
    "Principal",
    "PromptNotFoundError",
    "PromptRegistry",
    "PullRequestAction",
    "PullRequestRef",
    "QueueError",
    "RateLimitDecision",
    "RateLimiter",
    "RateLimiterBackendError",
    "RepositoryError",
    "SecretBackendError",
    "SecretNotFoundError",
    "Secrets",
    "SessionRepository",
    "ShellOutcome",
    "ShellTool",
    "TaskRepository",
    "TaskSubmitter",
    "TenantIsolationError",
    "TestOutcome",
    "TestTool",
    "TokenValidator",
    "ToolCall",
    "ToolCategory",
    "ToolContext",
    "ToolError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "ToolPermissionError",
    "ToolResult",
    "ToolSpec",
    "ToolValidationError",
    "UsageAggregate",
    "UsageGroupBy",
    "WorkspaceError",
    "WorkspaceManager",
]
