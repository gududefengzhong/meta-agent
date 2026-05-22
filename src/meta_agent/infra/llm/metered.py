"""Metering wrapper for :class:`LLMClient` adapters.

The wrapper transparently delegates to an inner :class:`LLMClient`
and, on every call, writes one :class:`LLMUsageRecord` through the
injected :class:`LLMUsageRepository`. It is the single enforcement
point for the L0 cost-visibility contract: as long as graphs receive
a metered client through :class:`GraphDeps`, no LLM call can escape
without an audit row.

Resilience contract:

* Recorder failures must **never** propagate into the business path.
  A flaky usage-log table cannot be allowed to break LLM calls; the
  wrapper logs a structured warning and swallows the recorder error.
* Errors raised by the inner client are recorded with
  ``status=ERROR`` and re-raised unchanged so retry / error-mapping
  logic upstream stays untouched.
* If no :class:`RequestContext` is bound, the wrapper logs a warning
  and skips recording (it cannot honestly attribute the call to a
  tenant). The LLM call itself is unaffected.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from meta_agent.core.domain.errors import ErrorCategory
from meta_agent.core.domain.llm_usage import LLMUsageRecord, LLMUsageStatus
from meta_agent.core.ports.llm import LLMClient, LLMError, LLMRequest, LLMResponse
from meta_agent.core.ports.llm_usage import LLMUsageRepository
from meta_agent.infra.security.context import RequestContext, get_current

logger = logging.getLogger(__name__)

_MAX_ERROR_MESSAGE_LEN = 500


class MeteredLLMClient(LLMClient):
    """Decorator that records every call made through ``inner``."""

    def __init__(
        self,
        inner: LLMClient,
        recorder: LLMUsageRepository,
        *,
        provider: str,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        record_id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not provider:
            raise ValueError("provider must be a non-empty string")
        self._inner = inner
        self._recorder = recorder
        self._provider = provider
        self._clock = clock if clock is not None else _utcnow
        self._monotonic = monotonic if monotonic is not None else time.perf_counter
        self._record_id_factory = (
            record_id_factory if record_id_factory is not None else _default_record_id
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        ctx = get_current()
        started = self._monotonic()
        try:
            response = await self._inner.complete(request)
        except LLMError as exc:
            elapsed_ms = _elapsed_ms(started, self._monotonic())
            await self._safe_record(
                ctx=ctx,
                request=request,
                response=None,
                error=exc,
                elapsed_ms=elapsed_ms,
            )
            raise
        elapsed_ms = _elapsed_ms(started, self._monotonic())
        await self._safe_record(
            ctx=ctx,
            request=request,
            response=response,
            error=None,
            elapsed_ms=elapsed_ms,
        )
        return response

    async def close(self) -> None:
        await self._inner.close()

    async def _safe_record(
        self,
        *,
        ctx: RequestContext | None,
        request: LLMRequest,
        response: LLMResponse | None,
        error: LLMError | None,
        elapsed_ms: int,
    ) -> None:
        if ctx is None:
            logger.warning(
                "llm.metered.skip_no_context",
                extra={"requested_model": request.model, "provider": self._provider},
            )
            return
        record = _build_record(
            ctx=ctx,
            request=request,
            response=response,
            error=error,
            elapsed_ms=elapsed_ms,
            provider=self._provider,
            record_id=self._record_id_factory(),
            now=self._clock(),
        )
        try:
            await self._recorder.record(record)
        except Exception as exc:
            logger.warning(
                "llm.metered.record_failed",
                extra={
                    "tenant_id": ctx.tenant_id,
                    "task_id": ctx.task_id,
                    "trace_id": ctx.trace_id,
                    "error_type": type(exc).__name__,
                },
            )


def _build_record(
    *,
    ctx: RequestContext,
    request: LLMRequest,
    response: LLMResponse | None,
    error: LLMError | None,
    elapsed_ms: int,
    provider: str,
    record_id: str,
    now: datetime,
) -> LLMUsageRecord:
    if response is not None:
        return LLMUsageRecord(
            record_id=record_id,
            tenant_id=ctx.tenant_id,
            trace_id=ctx.trace_id,
            request_id=ctx.request_id,
            principal_id=ctx.principal_id,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            provider=provider,
            model=response.model or None,
            requested_model=request.model,
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            total_tokens=response.usage.total_tokens,
            finish_reason=response.finish_reason,
            provider_response_id=response.provider_response_id,
            prompt_id=request.prompt_id,
            prompt_version=request.prompt_version,
            step_kind=request.step_kind,
            latency_ms=elapsed_ms,
            status=LLMUsageStatus.OK,
            created_at=now,
        )
    assert error is not None
    return LLMUsageRecord(
        record_id=record_id,
        tenant_id=ctx.tenant_id,
        trace_id=ctx.trace_id,
        request_id=ctx.request_id,
        principal_id=ctx.principal_id,
        session_id=ctx.session_id,
        task_id=ctx.task_id,
        provider=provider,
        model=None,
        requested_model=request.model,
        prompt_id=request.prompt_id,
        prompt_version=request.prompt_version,
        step_kind=request.step_kind,
        latency_ms=elapsed_ms,
        status=LLMUsageStatus.ERROR,
        error_category=_error_category(error),
        error_message=_truncate(str(error)),
        created_at=now,
    )


def _error_category(error: LLMError) -> ErrorCategory:
    return error.category


def _truncate(value: str) -> str:
    if len(value) <= _MAX_ERROR_MESSAGE_LEN:
        return value
    return value[: _MAX_ERROR_MESSAGE_LEN - 1] + "…"


def _elapsed_ms(started: float, ended: float) -> int:
    delta = ended - started
    if delta < 0:
        return 0
    return int(delta * 1000)


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _default_record_id() -> str:
    return f"llmu-{uuid.uuid4()}"
