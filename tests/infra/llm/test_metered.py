"""Unit tests for :class:`MeteredLLMClient`."""

from __future__ import annotations

import itertools
import logging
from datetime import UTC, datetime

import pytest

from meta_agent.core.domain import ErrorCategory, LLMUsageRecord, LLMUsageStatus
from meta_agent.core.ports.budget import BudgetUsage
from meta_agent.core.ports.llm import (
    ChatMessage,
    LLMRequest,
    LLMResponse,
    LLMTransientError,
    LLMUsage,
    MessageRole,
)
from meta_agent.core.ports.llm_usage import (
    LLMUsageFilter,
    LLMUsageRepository,
    UsageAggregate,
    UsageGroupBy,
)
from meta_agent.infra.llm.metered import MeteredLLMClient
from meta_agent.infra.security.context import RequestContext, bind_context
from tests.core.orchestration._fakes import FakeLLMClient


class _RecorderSpy(LLMUsageRepository):
    def __init__(self, *, fail: bool = False) -> None:
        self.records: list[LLMUsageRecord] = []
        self._fail = fail

    async def record(self, record: LLMUsageRecord) -> None:
        if self._fail:
            raise RuntimeError("db down")
        self.records.append(record)

    async def list_for_task(self, tenant_id: str, task_id: str) -> list[LLMUsageRecord]:
        return [r for r in self.records if r.tenant_id == tenant_id and r.task_id == task_id]

    async def aggregate_since(self, tenant_id: str, since: datetime) -> BudgetUsage:
        rows = [r for r in self.records if r.tenant_id == tenant_id and r.created_at >= since]
        tokens = sum(r.total_tokens or 0 for r in rows)
        cost = sum(r.cost_usd_micros or 0 for r in rows)
        return BudgetUsage(tokens_used=tokens, cost_usd_micros_used=cost)

    async def list_filtered(
        self,
        tenant_id: str,
        filt: LLMUsageFilter,
    ) -> list[LLMUsageRecord]:
        raise AssertionError("list_filtered not exercised by metered tests")

    async def aggregate_grouped(
        self,
        tenant_id: str,
        since: datetime,
        until: datetime,
        group_by: UsageGroupBy,
    ) -> list[UsageAggregate]:
        raise AssertionError("aggregate_grouped not exercised by metered tests")

    async def aggregate_for_task(
        self,
        tenant_id: str,
        task_id: str,
        group_by: UsageGroupBy,
    ) -> list[UsageAggregate]:
        raise AssertionError("aggregate_for_task not exercised by metered tests")


def _ctx(**overrides: str | None) -> RequestContext:
    base: dict[str, str | None] = {
        "tenant_id": "t-1",
        "principal_id": "p-1",
        "trace_id": "trace-1",
        "request_id": "req-1",
        "task_id": "task-1",
    }
    base.update(overrides)
    return RequestContext(**base)  # type: ignore[arg-type]


def _request() -> LLMRequest:
    return LLMRequest(
        messages=(ChatMessage(role=MessageRole.USER, content="hi"),),
        model="openai/gpt-4o",
    )


def _ok_response() -> LLMResponse:
    return LLMResponse(
        content="hello",
        model="openai/gpt-4o",
        finish_reason="stop",
        usage=LLMUsage(prompt_tokens=3, completion_tokens=5, total_tokens=8),
        provider_response_id="gen_abc",
    )


def _make_client(
    inner: FakeLLMClient | None = None,
    recorder: _RecorderSpy | None = None,
    *,
    fail_record: bool = False,
) -> tuple[MeteredLLMClient, _RecorderSpy, FakeLLMClient]:
    inner = inner or FakeLLMClient(response=_ok_response())
    recorder = recorder or _RecorderSpy(fail=fail_record)
    ticks = itertools.count(start=0, step=1)
    ids = itertools.count(start=1)
    client = MeteredLLMClient(
        inner,
        recorder,
        provider="openrouter",
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        monotonic=lambda: next(ticks) * 0.05,
        record_id_factory=lambda: f"llmu-{next(ids)}",
    )
    return client, recorder, inner


async def test_ok_path_records_tokens_and_returns_response() -> None:
    client, recorder, _ = _make_client()
    with bind_context(_ctx()):
        response = await client.complete(_request())
    assert response.content == "hello"
    assert len(recorder.records) == 1
    r = recorder.records[0]
    assert r.tenant_id == "t-1"
    assert r.task_id == "task-1"
    assert r.provider == "openrouter"
    assert r.model == "openai/gpt-4o"
    assert r.requested_model == "openai/gpt-4o"
    assert r.prompt_tokens == 3
    assert r.completion_tokens == 5
    assert r.total_tokens == 8
    assert r.status is LLMUsageStatus.OK
    assert r.latency_ms == 50


async def test_error_path_records_and_reraises() -> None:
    def boom(_req: LLMRequest) -> LLMResponse:
        raise LLMTransientError("upstream 503")

    inner = FakeLLMClient(handler=boom)
    client, recorder, _ = _make_client(inner=inner)
    with bind_context(_ctx()), pytest.raises(LLMTransientError):
        await client.complete(_request())
    assert len(recorder.records) == 1
    r = recorder.records[0]
    assert r.status is LLMUsageStatus.ERROR
    assert r.error_category is ErrorCategory.TRANSIENT
    assert r.error_message == "upstream 503"
    assert r.model is None
    assert r.requested_model == "openai/gpt-4o"


async def test_missing_context_skips_recording_but_keeps_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, recorder, _ = _make_client()
    with caplog.at_level(logging.WARNING, logger="meta_agent.infra.llm.metered"):
        response = await client.complete(_request())
    assert response.content == "hello"
    assert recorder.records == []
    assert any("skip_no_context" in rec.getMessage() for rec in caplog.records)


async def test_recorder_failure_does_not_break_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, recorder, _ = _make_client(fail_record=True)
    with (
        bind_context(_ctx()),
        caplog.at_level(logging.WARNING, logger="meta_agent.infra.llm.metered"),
    ):
        response = await client.complete(_request())
    assert response.content == "hello"
    assert recorder.records == []
    assert any("record_failed" in rec.getMessage() for rec in caplog.records)


async def test_ok_path_records_step_kind_from_request() -> None:
    client, recorder, _ = _make_client()
    request = LLMRequest(
        messages=(ChatMessage(role=MessageRole.USER, content="hi"),),
        model="openai/gpt-4o",
        step_kind="plan",
        prompt_id="x.y",
        prompt_version=1,
    )
    with bind_context(_ctx()):
        await client.complete(request)
    assert len(recorder.records) == 1
    r = recorder.records[0]
    assert r.step_kind == "plan"
    assert r.prompt_id == "x.y"
    assert r.prompt_version == 1


async def test_error_path_records_step_kind_from_request() -> None:
    def boom(_req: LLMRequest) -> LLMResponse:
        raise LLMTransientError("upstream 503")

    inner = FakeLLMClient(handler=boom)
    client, recorder, _ = _make_client(inner=inner)
    request = LLMRequest(
        messages=(ChatMessage(role=MessageRole.USER, content="hi"),),
        model="openai/gpt-4o",
        step_kind="edit",
    )
    with bind_context(_ctx()), pytest.raises(LLMTransientError):
        await client.complete(request)
    assert len(recorder.records) == 1
    assert recorder.records[0].step_kind == "edit"


async def test_close_delegates_to_inner() -> None:
    client, _, inner = _make_client()
    await client.close()
    assert inner.closed is True
