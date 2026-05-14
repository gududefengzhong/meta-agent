"""Unit tests for structured logging."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest

from meta_agent.infra.observability.logging import (
    CONTEXT_LOG_KEYS,
    ContextFilter,
    JsonFormatter,
    _reset_for_testing,
    configure_logging,
    get_logger,
)
from meta_agent.infra.security import RequestContext, bind_context


def _ctx(**overrides: object) -> RequestContext:
    base: dict[str, object] = {
        "tenant_id": "t-1",
        "principal_id": "p-1",
        "trace_id": "trace-1",
        "request_id": "req-1",
    }
    base.update(overrides)
    return RequestContext(**base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    _reset_for_testing()
    yield
    _reset_for_testing()


def _make_record() -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )


def test_context_filter_sets_none_when_unbound() -> None:
    record = _make_record()
    assert ContextFilter().filter(record) is True
    for key in CONTEXT_LOG_KEYS:
        assert getattr(record, key) is None


def test_context_filter_injects_bound_fields() -> None:
    record = _make_record()
    with bind_context(_ctx(task_id="task-1")):
        ContextFilter().filter(record)
    assert record.tenant_id == "t-1"  # type: ignore[attr-defined]
    assert record.task_id == "task-1"  # type: ignore[attr-defined]
    assert record.session_id is None  # type: ignore[attr-defined]


def test_json_formatter_emits_required_keys() -> None:
    record = _make_record()
    ContextFilter().filter(record)
    with bind_context(_ctx(task_id="task-1")):
        ContextFilter().filter(record)
    line = JsonFormatter().format(record)
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["message"] == "hello"
    assert payload["tenant_id"] == "t-1"
    assert payload["task_id"] == "task-1"
    # None fields should be omitted.
    assert "session_id" not in payload


def test_configure_logging_writes_json_line() -> None:
    stream = io.StringIO()
    configure_logging(level=logging.INFO, stream=stream)
    logger = get_logger("meta_agent.test")
    with bind_context(_ctx(task_id="task-1")):
        logger.info("hi there")
    output = stream.getvalue().strip()
    assert output, "expected a log line on the stream"
    payload = json.loads(output)
    assert payload["message"] == "hi there"
    assert payload["tenant_id"] == "t-1"
    assert payload["task_id"] == "task-1"


def test_configure_logging_is_idempotent() -> None:
    stream = io.StringIO()
    configure_logging(stream=stream)
    handlers_before = list(logging.getLogger().handlers)
    configure_logging(stream=stream)
    handlers_after = list(logging.getLogger().handlers)
    assert handlers_before == handlers_after
