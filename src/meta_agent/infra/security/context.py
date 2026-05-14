"""Request-scoped context propagation.

Implements the identifier contract defined in
``docs/specs/CONTEXT_PROPAGATION.md`` §1. The context is held in a
single :class:`contextvars.ContextVar` so it propagates naturally
across ``await`` boundaries and across ``asyncio.Task`` boundaries.

The context object is intentionally a plain frozen ``dataclass`` rather
than a Pydantic model: it is hot-path data, not a value object that
needs validation or serialization. Producers (HTTP middleware, MQ
consumers) are responsible for constructing valid instances.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import Any, Final

from meta_agent.core.domain import AgentError, ErrorCategory


class MissingContextError(AgentError):
    """Raised when required context is read but none is bound.

    This is a programming error (the producer middleware failed to
    bind the context). It is therefore categorised as
    :class:`ErrorCategory.LOGIC`, not :class:`ErrorCategory.VALIDATION`.
    """

    category = ErrorCategory.LOGIC


@dataclass(frozen=True, slots=True)
class RequestContext:
    """A request-scoped bundle of identifiers.

    Contains every identifier required by
    ``docs/specs/CONTEXT_PROPAGATION.md`` §1. Fields with ``None``
    defaults are not required in every flow (for example, ``task_id``
    is only set once a task has been created).
    """

    tenant_id: str
    principal_id: str
    trace_id: str
    request_id: str
    session_id: str | None = None
    task_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    idempotency_key: str | None = None


_CURRENT: Final[ContextVar[RequestContext | None]] = ContextVar(
    "meta_agent_request_context",
    default=None,
)


def get_current() -> RequestContext | None:
    """Return the current bound context, or ``None`` if unset."""
    return _CURRENT.get()


def require_current() -> RequestContext:
    """Return the current bound context or raise :class:`MissingContextError`.

    Call this from any code path that writes multi-tenant state to
    enforce the L0 isolation contract. The caller does not need to
    handle ``None`` explicitly.
    """
    ctx = _CURRENT.get()
    if ctx is None:
        raise MissingContextError("RequestContext is not bound for the current execution")
    return ctx


def require_tenant_id() -> str:
    """Convenience accessor: the current tenant_id, or raise."""
    return require_current().tenant_id


@contextmanager
def bind_context(context: RequestContext) -> Iterator[RequestContext]:
    """Bind ``context`` for the duration of the ``with`` block.

    Restores the prior binding on exit (including the unbound state).
    Safe to nest; safe to use concurrently across asyncio tasks
    because :mod:`contextvars` is task-local.
    """
    token: Token[RequestContext | None] = _CURRENT.set(context)
    try:
        yield context
    finally:
        _CURRENT.reset(token)


@contextmanager
def update_context(**fields: Any) -> Iterator[RequestContext]:
    """Bind a derived context with selected fields replaced.

    The caller must already have a bound context; this is a structured
    way to add ``task_id`` / ``span_id`` once they become known
    without losing the rest of the bundle.
    """
    current = require_current()
    derived = replace(current, **fields)
    with bind_context(derived) as bound:
        yield bound
