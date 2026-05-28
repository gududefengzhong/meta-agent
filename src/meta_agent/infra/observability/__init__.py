"""Observability layer: structured logging, tracing, metrics.

【当前】结构化日志 + OTel tracing SDK 接入抽象。
【目标】完整 OTel 指标、错误上报、日志聚合后端对接。
"""

from meta_agent.infra.observability.langfuse_exporter import (
    LangfuseConfig,
    LangfuseExporterError,
    LangfuseExportResult,
    LangfuseTrajectoryExporter,
)
from meta_agent.infra.observability.logging import (
    CONTEXT_LOG_KEYS,
    ContextFilter,
    JsonFormatter,
    configure_logging,
    get_logger,
)
from meta_agent.infra.observability.tracing import (
    configure_tracing,
    get_tracer,
    start_span,
)

__all__ = [
    "CONTEXT_LOG_KEYS",
    "ContextFilter",
    "JsonFormatter",
    "LangfuseConfig",
    "LangfuseExportResult",
    "LangfuseExporterError",
    "LangfuseTrajectoryExporter",
    "configure_logging",
    "configure_tracing",
    "get_logger",
    "get_tracer",
    "start_span",
]
