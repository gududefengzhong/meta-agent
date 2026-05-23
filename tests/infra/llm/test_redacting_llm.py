"""End-to-end tests for :class:`RedactingLLMClient`."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import UTC, datetime

from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.ports.audit_sink import AuditSink
from meta_agent.core.ports.llm import (
    ChatMessage,
    LLMClient,
    LLMRequest,
    LLMResponse,
    MessageRole,
)
from meta_agent.core.ports.tools import ToolCall
from meta_agent.infra.llm.redacting import RedactingLLMClient
from meta_agent.infra.redaction.redactor import Redactor
from meta_agent.infra.security.context import RequestContext, bind_context


def _id_factory() -> Callable[[], str]:
    counter = itertools.count(1)
    return lambda: f"id-{next(counter)}"


class _CapturingInner(LLMClient):
    def __init__(self, response: LLMResponse) -> None:
        self.received: list[LLMRequest] = []
        self._response = response

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.received.append(request)
        return self._response

    async def close(self) -> None:
        pass


class _CapturingAudit(AuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> None:
        self.events.append(event)


def _ctx() -> RequestContext:
    return RequestContext(
        tenant_id="t-1",
        principal_id="user-1",
        trace_id="trace-1",
        request_id="req-1",
    )


def _bare_response(content: str = "ok") -> LLMResponse:
    return LLMResponse(content=content, model="fake/model", finish_reason="stop")


async def test_secret_in_user_message_redacted_before_inner_call() -> None:
    inner = _CapturingInner(_bare_response())
    client = RedactingLLMClient(inner, redactor=Redactor())
    request = LLMRequest(
        messages=(ChatMessage(role=MessageRole.USER, content="key: ghp_" + "a" * 40),)
    )
    with bind_context(_ctx()):
        await client.complete(request)
    assert len(inner.received) == 1
    forwarded = inner.received[0].messages[0].content
    assert "ghp_" not in forwarded
    assert "[REDACTED:github_token]" in forwarded


async def test_secret_in_response_redacted_before_returning() -> None:
    leaked = "echo: sk-" + "a" * 30
    inner = _CapturingInner(_bare_response(content=leaked))
    client = RedactingLLMClient(inner, redactor=Redactor())
    request = LLMRequest(messages=(ChatMessage(role=MessageRole.USER, content="hi"),))
    with bind_context(_ctx()):
        response = await client.complete(request)
    assert "sk-" not in response.content
    assert "[REDACTED:openai_key]" in response.content


async def test_other_request_fields_preserved() -> None:
    inner = _CapturingInner(_bare_response())
    client = RedactingLLMClient(inner, redactor=Redactor())
    request = LLMRequest(
        messages=(ChatMessage(role=MessageRole.USER, content="hi"),),
        model="anthropic/claude",
        temperature=0.3,
        max_tokens=100,
        prompt_id="bug_fix.plan.system",
        prompt_version=2,
        step_kind="plan",
    )
    with bind_context(_ctx()):
        await client.complete(request)
    forwarded = inner.received[0]
    assert forwarded.model == "anthropic/claude"
    assert forwarded.temperature == 0.3
    assert forwarded.max_tokens == 100
    assert forwarded.prompt_id == "bug_fix.plan.system"
    assert forwarded.prompt_version == 2
    assert forwarded.step_kind == "plan"


async def test_response_tool_call_arguments_scrubbed() -> None:
    leaked_args = {"url": "postgres://u:secret123@host/db", "limit": 5}
    response = LLMResponse(
        content="",
        model="fake/model",
        finish_reason="tool_call",
        tool_calls=(ToolCall(id="tc-1", name="web_fetch", arguments=leaked_args),),
    )
    inner = _CapturingInner(response)
    client = RedactingLLMClient(inner, redactor=Redactor())
    request = LLMRequest(messages=(ChatMessage(role=MessageRole.USER, content="x"),))
    with bind_context(_ctx()):
        resp = await client.complete(request)
    tc = resp.tool_calls[0]
    assert isinstance(tc.arguments["url"], str)
    assert "secret123" not in tc.arguments["url"]
    # Non-string args pass through unchanged.
    assert tc.arguments["limit"] == 5


async def test_audit_emitted_only_when_redaction_actually_fired() -> None:
    inner = _CapturingInner(_bare_response())
    audit = _CapturingAudit()
    client = RedactingLLMClient(
        inner,
        redactor=Redactor(),
        audit_sink=audit,
        clock=lambda: datetime(2026, 6, 23, tzinfo=UTC),
        id_factory=_id_factory(),
    )
    # Clean request: no audit emitted.
    with bind_context(_ctx()):
        await client.complete(
            LLMRequest(messages=(ChatMessage(role=MessageRole.USER, content="clean prompt"),))
        )
    assert audit.events == []
    # Dirty request: one audit row for the redaction hit.
    with bind_context(_ctx()):
        await client.complete(
            LLMRequest(
                messages=(
                    ChatMessage(
                        role=MessageRole.USER,
                        content="leaked: ghp_" + "b" * 40,
                    ),
                )
            )
        )
    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.action == "llm.redaction.applied_to_request"
    assert event.payload["hits"] == {"github_token": 1}


async def test_audit_skipped_when_no_context_bound() -> None:
    """The audit hook needs a bound context to attribute the event.

    Calling without ``bind_context`` (ad-hoc smoke harness) must
    still redact but skip the audit emission rather than raising.
    """

    inner = _CapturingInner(_bare_response())
    audit = _CapturingAudit()
    client = RedactingLLMClient(
        inner,
        redactor=Redactor(),
        audit_sink=audit,
        clock=lambda: datetime(2026, 6, 23, tzinfo=UTC),
        id_factory=_id_factory(),
    )
    # No bind_context wrapper — direct invocation.
    await client.complete(
        LLMRequest(messages=(ChatMessage(role=MessageRole.USER, content="leak ghp_" + "c" * 40),))
    )
    # Redaction still happened (verified by inner.received) but no audit row.
    forwarded = inner.received[0].messages[0].content
    assert "[REDACTED:github_token]" in forwarded
    assert audit.events == []


async def test_close_propagates_to_inner() -> None:
    closed = {"hit": False}

    class _Inner(LLMClient):
        async def complete(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover
            raise AssertionError

        async def close(self) -> None:
            closed["hit"] = True

    client = RedactingLLMClient(_Inner(), redactor=Redactor())
    await client.close()
    assert closed["hit"] is True
