"""Unit tests for OpenTelemetry tracing wiring."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from meta_agent.infra.observability.tracing import (
    _reset_for_testing,
    configure_tracing,
    start_span,
)
from meta_agent.infra.security import RequestContext, bind_context

# OpenTelemetry forbids replacing the global TracerProvider once set, so
# install a single SDK provider for the whole module and clear the
# in-memory exporter between tests.
_IN_MEMORY_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_IN_MEMORY_EXPORTER))
trace.set_tracer_provider(_PROVIDER)


def _ctx(**overrides: object) -> RequestContext:
    base: dict[str, object] = {
        "tenant_id": "t-1",
        "principal_id": "p-1",
        "trace_id": "trace-1",
        "request_id": "req-1",
    }
    base.update(overrides)
    return RequestContext(**base)  # type: ignore[arg-type]


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    """Expose the module-scoped in-memory exporter with a clean slate."""
    _IN_MEMORY_EXPORTER.clear()
    yield _IN_MEMORY_EXPORTER
    _IN_MEMORY_EXPORTER.clear()


def test_start_span_records_span_name(exporter: InMemorySpanExporter) -> None:
    with start_span("unit.test"):
        pass
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "unit.test"


def test_start_span_attaches_context_attributes(
    exporter: InMemorySpanExporter,
) -> None:
    with bind_context(_ctx(task_id="task-1", session_id="sess-1")), start_span("work"):
        pass
    span = exporter.get_finished_spans()[0]
    attrs = dict(span.attributes or {})
    assert attrs["tenant_id"] == "t-1"
    assert attrs["task_id"] == "task-1"
    assert attrs["session_id"] == "sess-1"
    assert "span_id" not in attrs  # not in CONTEXT_SPAN_ATTRS


def test_caller_attributes_override_context(
    exporter: InMemorySpanExporter,
) -> None:
    with (
        bind_context(_ctx(tenant_id="ctx-tenant")),
        start_span("work", attributes={"tenant_id": "override"}),
    ):
        pass
    span = exporter.get_finished_spans()[0]
    attrs = dict(span.attributes or {})
    assert attrs["tenant_id"] == "override"


def test_configure_tracing_is_idempotent() -> None:
    # The module-level fixture has already installed a TracerProvider.
    # OpenTelemetry forbids replacing the global provider, so
    # ``configure_tracing`` must short-circuit on its internal flag and
    # leave the existing provider in place.
    _reset_for_testing()
    before = trace.get_tracer_provider()
    configure_tracing("svc-a")
    configure_tracing("svc-b")
    after = trace.get_tracer_provider()
    assert before is after
