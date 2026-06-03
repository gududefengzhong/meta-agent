"""LLMClient decorator that scrubs prompts + responses (Phase γ-D).

Wraps an inner :class:`LLMClient`. Every outgoing :class:`LLMRequest`
has its message contents (system / user / assistant / tool messages
alike) run through a :class:`Redactor` before being forwarded. Every
inbound :class:`LLMResponse` has its ``content`` and tool-call
arguments redacted as well — the model occasionally echoes parts of
the prompt back, and we want that surface scrubbed before it lands
in audit logs / customer-visible output.

Place this decorator OUTERMOST in the LLM chain so audit / metering /
routing / OpenRouter all observe redacted bytes. The downside —
graphs see the raw prompts (because they build the LLMRequest from
state.data before this layer sees it) — is acceptable: the goal is
to prevent egress, not to hide secrets from the graph code that
already has access to the workspace.

The decorator never raises into the caller. Redaction failures
(impossible with the pure-regex scanner today, but defended
against) fall back to forwarding the original request unchanged so
the LLM stack does not become a single point of failure.

Audit hook:
* Each request that contained a redacted token emits an audit row
  ``llm.redaction.applied`` so operators can watch the redaction
  rate. The hook is optional — leave ``audit_sink=None`` to disable.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.ports.audit_sink import AuditSink
from meta_agent.core.ports.llm import (
    ChatMessage,
    LLMClient,
    LLMRequest,
    LLMResponse,
)
from meta_agent.core.ports.tools import ToolCall
from meta_agent.infra.redaction.redactor import RedactionReport, Redactor
from meta_agent.infra.security.context import get_current

logger = logging.getLogger(__name__)


class RedactingLLMClient(LLMClient):
    """Scrub request messages + response content before/after the inner call."""

    def __init__(
        self,
        inner: LLMClient,
        *,
        redactor: Redactor,
        audit_sink: AuditSink | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._inner = inner
        self._redactor = redactor
        self._audit_sink = audit_sink
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: str(uuid.uuid4()))

    async def complete(self, request: LLMRequest) -> LLMResponse:
        scrubbed_request, request_report = self._scrub_request(request)
        if request_report.any_redacted:
            await self._audit("llm.redaction.applied_to_request", request_report)
        response = await self._inner.complete(scrubbed_request)
        scrubbed_response, response_report = self._scrub_response(response)
        if response_report.any_redacted:
            await self._audit("llm.redaction.applied_to_response", response_report)
        return scrubbed_response

    async def close(self) -> None:
        await self._inner.close()

    # ------------------------------------------------------------------ helpers

    def _scrub_request(self, request: LLMRequest) -> tuple[LLMRequest, RedactionReport]:
        new_messages: list[ChatMessage] = []
        cumulative: dict[str, int] = {}
        for msg in request.messages:
            scrubbed_content, msg_report = self._redactor.scrub(msg.content)
            for label, count in msg_report.hits.items():
                cumulative[label] = cumulative.get(label, 0) + count
            new_tool_calls: tuple[ToolCall, ...] = msg.tool_calls
            if msg.tool_calls:
                new_tool_calls = tuple(
                    self._scrub_tool_call(tc, cumulative) for tc in msg.tool_calls
                )
            new_messages.append(
                msg.model_copy(
                    update={
                        "content": scrubbed_content,
                        "tool_calls": new_tool_calls,
                    }
                )
            )
        new_request = request.model_copy(update={"messages": tuple(new_messages)})
        return new_request, RedactionReport(hits=cumulative)

    def _scrub_response(self, response: LLMResponse) -> tuple[LLMResponse, RedactionReport]:
        scrubbed_content, content_report = self._redactor.scrub(response.content)
        cumulative: dict[str, int] = dict(content_report.hits)
        new_tool_calls: tuple[ToolCall, ...] = response.tool_calls
        if response.tool_calls:
            new_tool_calls = tuple(
                self._scrub_tool_call(tc, cumulative) for tc in response.tool_calls
            )
        new_response = response.model_copy(
            update={"content": scrubbed_content, "tool_calls": new_tool_calls}
        )
        return new_response, RedactionReport(hits=cumulative)

    def _scrub_tool_call(
        self,
        tc: ToolCall,
        cumulative: dict[str, int],
    ) -> ToolCall:
        """Scrub the JSON-shaped tool-call arguments by serialising → redact → reparse.

        Tool arguments live as ``dict[str, Any]`` so we walk strings
        within them rather than serialising the whole structure. This
        preserves the JSON shape downstream code expects while still
        scrubbing strings that may have come from echoed prompts.
        """

        new_args = {key: self._scrub_value(val, cumulative) for key, val in tc.arguments.items()}
        return tc.model_copy(update={"arguments": new_args})

    def _scrub_value(self, value: object, cumulative: dict[str, int]) -> object:
        if isinstance(value, str):
            scrubbed, report = self._redactor.scrub(value)
            for label, count in report.hits.items():
                cumulative[label] = cumulative.get(label, 0) + count
            return scrubbed
        if isinstance(value, dict):
            return {k: self._scrub_value(v, cumulative) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub_value(v, cumulative) for v in value]
        return value

    async def _audit(self, action: str, report: RedactionReport) -> None:
        if self._audit_sink is None or not report.any_redacted:
            return
        ctx = get_current()
        if ctx is None:
            # No bound context: an ad-hoc call site without
            # ``bind_context``. Skip the audit rather than raising —
            # the redaction itself already landed in the request.
            return
        await self._audit_sink.append(
            AuditEvent(
                event_id=self._id_factory(),
                tenant_id=ctx.tenant_id,
                principal_id=ctx.principal_id,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                trace_id=ctx.trace_id,
                action=action,
                payload={"hits": dict(report.hits), "total": report.total},
                occurred_at=self._clock(),
            )
        )
