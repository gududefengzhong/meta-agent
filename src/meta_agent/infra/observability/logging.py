"""Structured JSON logging with automatic context propagation.

Per ``docs/specs/CONTEXT_PROPAGATION.md`` §2.4 every log record must
carry the fixed key set (``tenant_id`` etc.) so that records emitted
from different processes can be joined back to a single request or
task. This module wires a :class:`logging.Filter` that reads from the
current :class:`RequestContext` and a :class:`logging.Formatter` that
emits one-line JSON.

Production callers should call :func:`configure_logging` exactly once
at process startup; calling it again is a no-op. Tests can reset the
state via :func:`_reset_for_testing`.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, TextIO

from meta_agent.infra.security.context import get_current

CONTEXT_LOG_KEYS: tuple[str, ...] = (
    "tenant_id",
    "principal_id",
    "session_id",
    "task_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "request_id",
    "idempotency_key",
)


class ContextFilter(logging.Filter):
    """Inject the bound :class:`RequestContext` fields onto each record.

    Missing fields (no context bound, or optional field unset) are
    written as ``None`` so downstream formatters can decide whether to
    emit them.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        ctx = get_current()
        for key in CONTEXT_LOG_KEYS:
            value = getattr(ctx, key, None) if ctx is not None else None
            setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    """One-line JSON formatter producing log-aggregation friendly output."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in CONTEXT_LOG_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


_configured: bool = False


def configure_logging(level: int = logging.INFO, stream: TextIO | None = None) -> None:
    """Install the JSON formatter and context filter on the root logger.

    Idempotent. The default stream is :data:`sys.stderr` which is the
    convention for structured logs in containerised environments.
    """
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    _configured = True


def _reset_for_testing() -> None:
    """Reset the module-level configured flag. Intended for tests only."""
    global _configured
    _configured = False
    logging.getLogger().handlers = []


def get_logger(name: str) -> logging.Logger:
    """Return a named logger; use this rather than ``logging.getLogger``.

    Encourages centralised configuration and gives a single point at
    which structured-logging conventions can be revisited later.
    """
    return logging.getLogger(name)
