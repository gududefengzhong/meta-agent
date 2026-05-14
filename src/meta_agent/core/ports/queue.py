"""Queue ports: producer and consumer abstractions.

Concrete adapters live under :mod:`meta_agent.infra.queue`. The default
adapter targets Redis Streams (see ``docs/specs/INFRA_SELECTION_MATRIX.md``);
the ports are kept neutral so a future migration to NATS/Kafka does not
ripple into business code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from meta_agent.core.domain.errors import AgentError, ErrorCategory
from meta_agent.core.ports.message import MessageEnvelope, MessageHandler


class QueueError(AgentError):
    """Base class for queue-adapter errors.

    Default category is :class:`ErrorCategory.TRANSIENT`, hence
    retryable. Adapters raise subclasses with a different category for
    non-retryable cases (e.g. malformed broker response).
    """

    category = ErrorCategory.TRANSIENT


class MessagePublisher(ABC):
    """Publishes a :class:`MessageEnvelope` to a logical topic.

    Implementations must be safe to call concurrently from multiple
    asyncio tasks. They are expected to be idempotent at the broker
    level (i.e. delivering the same envelope twice does not duplicate
    server-side state) but consumers must still rely on
    ``idempotency_key`` for end-to-end deduplication.
    """

    @abstractmethod
    async def publish(self, envelope: MessageEnvelope) -> None:
        """Publish ``envelope`` and return once durably accepted."""


class MessageConsumer(ABC):
    """Consumes messages from a logical topic via a consumer group.

    A single :class:`MessageConsumer` instance binds to one ``(topic,
    group, consumer_name)`` triple. Multiple replicas of the same
    consumer group share the workload; per-message processing is
    expected to be idempotent because at-least-once delivery is the
    default semantic.
    """

    @abstractmethod
    async def start(self, handler: MessageHandler) -> None:
        """Start consuming and dispatch each message to ``handler``.

        Returns when :meth:`stop` is invoked from another task or when
        the underlying broker connection is closed.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Signal the consumer loop to stop and wait for it to drain."""
