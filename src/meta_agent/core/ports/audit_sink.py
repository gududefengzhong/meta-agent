"""Minimal append-only sink for :class:`AuditEvent`.

Carved out of :class:`AuditRepository` so non-storage producers (the
rate-limit and circuit-breaker decorators, future ingress middlewares)
only depend on the single ``append`` capability and never see the
read path. :class:`AuditRepository` extends :class:`AuditSink`, so
existing wiring keeps working unchanged.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from meta_agent.core.domain.audit import AuditEvent


class AuditSink(ABC):
    """Append-only producer surface for audit events.

    Implementations must be safe to call concurrently. Producers treat
    :meth:`append` as best-effort: a failure here must never abort the
    business operation that triggered the audit record.
    """

    @abstractmethod
    async def append(self, event: AuditEvent) -> None: ...


__all__ = ["AuditSink"]
