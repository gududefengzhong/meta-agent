"""OpenTelemetry tracing wiring.

The OpenTelemetry API surface is itself an abstraction layer; we do
not add another Port on top of it. Production deployments should call
:func:`configure_tracing` once at process startup with an OTLP
exporter; tests and PoC deployments can omit the exporter (spans are
still recorded by the SDK but discarded).

Span attributes mirror the identifier contract in
``docs/specs/CONTEXT_PROPAGATION.md`` §2.4: every span carries the
context attributes that are not ``None``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Span, Tracer

from meta_agent.infra.security.context import get_current

CONTEXT_SPAN_ATTRS: tuple[str, ...] = (
    "tenant_id",
    "principal_id",
    "session_id",
    "task_id",
    "request_id",
    "idempotency_key",
)

_configured: bool = False
_DEFAULT_TRACER_NAME = "meta_agent"


def configure_tracing(
    service_name: str,
    exporter: SpanExporter | None = None,
) -> None:
    """Install a :class:`TracerProvider` on the global tracer factory.

    The resource carries ``service.name`` per OpenTelemetry semantic
    conventions. Without an exporter, spans are recorded but
    discarded; this is acceptable for unit tests and PoC runs.
    Idempotent.
    """
    global _configured
    if _configured:
        return
    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    if exporter is not None:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _configured = True


def _reset_for_testing() -> None:
    """Reset the configured flag and install a fresh no-op provider.

    Tests should call this in fixtures to avoid leaking state. The OTel
    SDK does not officially support resetting the global provider; the
    workaround installs a fresh :class:`TracerProvider` without
    processors so subsequent ``configure_tracing`` calls re-take effect.
    """
    global _configured
    _configured = False
    trace.set_tracer_provider(TracerProvider())


def get_tracer(name: str = _DEFAULT_TRACER_NAME) -> Tracer:
    """Return the tracer for ``name``."""
    return trace.get_tracer(name)


@contextmanager
def start_span(
    name: str,
    *,
    attributes: Mapping[str, Any] | None = None,
    tracer_name: str = _DEFAULT_TRACER_NAME,
) -> Iterator[Span]:
    """Start a span enriched with the current :class:`RequestContext`.

    Context fields that are ``None`` are not attached. Caller-supplied
    ``attributes`` override context-derived values on key collision.
    """
    tracer = get_tracer(tracer_name)
    merged: dict[str, Any] = {}
    ctx = get_current()
    if ctx is not None:
        for key in CONTEXT_SPAN_ATTRS:
            value = getattr(ctx, key, None)
            if value is not None:
                merged[key] = value
    if attributes:
        merged.update(attributes)
    with tracer.start_as_current_span(name, attributes=merged) as span:
        yield span
