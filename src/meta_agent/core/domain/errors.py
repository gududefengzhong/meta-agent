"""Unified error model.

Per ``docs/specs/AGENT_SPEC.md`` §代码与运维规范, all failure-prone
paths must declare explicit error categories and retry intent. This
module defines the base exception class and the canonical category
enum; concrete subclasses live next to the modules that raise them.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCategory(StrEnum):
    """Canonical failure taxonomy.

    The category drives retry policy, alerting, and user-facing
    messaging. A subclass of :class:`AgentError` should pick exactly
    one category.
    """

    TRANSIENT = "transient"
    """Retriable; expected to succeed on retry (e.g. brief upstream timeout)."""

    EXTERNAL = "external"
    """External dependency permanently or persistently failing for this call."""

    PERMISSION = "permission"
    """Authorization or tenancy violation; never retry, escalate."""

    VALIDATION = "validation"
    """Caller supplied invalid input; never retry without changes."""

    LOGIC = "logic"
    """Internal programming error or invariant violation; never retry blindly."""

    USER_CANCELLED = "user_cancelled"
    """User or human reviewer explicitly stopped the work."""


class AgentError(Exception):
    """Base class for all domain-level errors raised inside the agent.

    Subclasses should set :attr:`category` to the most specific
    matching :class:`ErrorCategory`. The ``retryable`` property is
    derived from the category to keep retry decisions consistent.
    """

    category: ErrorCategory = ErrorCategory.LOGIC

    def __init__(self, message: str, *, category: ErrorCategory | None = None) -> None:
        super().__init__(message)
        if category is not None:
            self.category = category

    @property
    def retryable(self) -> bool:
        """Whether this error category permits a default retry attempt."""
        return self.category == ErrorCategory.TRANSIENT
