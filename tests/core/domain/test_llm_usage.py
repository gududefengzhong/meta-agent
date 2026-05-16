"""Unit tests for the LLMUsageRecord model."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from meta_agent.core.domain import ErrorCategory, LLMUsageRecord, LLMUsageStatus


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def test_llm_usage_record_minimum_required_fields() -> None:
    record = LLMUsageRecord(
        record_id="llmu-1",
        tenant_id="t-1",
        trace_id="trace-1",
        provider="openrouter",
        latency_ms=42,
        status=LLMUsageStatus.OK,
        created_at=_now(),
    )
    assert record.task_id is None
    assert record.model is None
    assert record.prompt_tokens is None
    assert record.cost_usd_micros is None
    assert record.error_category is None


def test_llm_usage_record_full_ok_path() -> None:
    record = LLMUsageRecord(
        record_id="llmu-2",
        tenant_id="t-1",
        trace_id="trace-1",
        request_id="req-1",
        principal_id="p-1",
        session_id="s-1",
        task_id="task-1",
        provider="openrouter",
        model="openai/gpt-4o",
        requested_model="openai/gpt-4o",
        prompt_tokens=12,
        completion_tokens=34,
        total_tokens=46,
        finish_reason="stop",
        provider_response_id="gen_abc",
        cost_usd_micros=1500,
        latency_ms=210,
        status=LLMUsageStatus.OK,
        created_at=_now(),
    )
    assert record.total_tokens == 46
    assert record.cost_usd_micros == 1500


def test_llm_usage_record_error_path_keeps_category() -> None:
    record = LLMUsageRecord(
        record_id="llmu-3",
        tenant_id="t-1",
        trace_id="trace-1",
        provider="openrouter",
        requested_model="openai/gpt-4o",
        latency_ms=11,
        status=LLMUsageStatus.ERROR,
        error_category=ErrorCategory.TRANSIENT,
        error_message="upstream 503",
        created_at=_now(),
    )
    assert record.status is LLMUsageStatus.ERROR
    assert record.error_category is ErrorCategory.TRANSIENT


def test_llm_usage_record_rejects_negative_tokens() -> None:
    with pytest.raises(ValidationError):
        LLMUsageRecord(
            record_id="llmu-4",
            tenant_id="t-1",
            trace_id="trace-1",
            provider="openrouter",
            prompt_tokens=-1,
            latency_ms=0,
            status=LLMUsageStatus.OK,
            created_at=_now(),
        )


def test_llm_usage_record_is_frozen() -> None:
    record = LLMUsageRecord(
        record_id="llmu-5",
        tenant_id="t-1",
        trace_id="trace-1",
        provider="openrouter",
        latency_ms=0,
        status=LLMUsageStatus.OK,
        created_at=_now(),
    )
    with pytest.raises(ValidationError):
        record.tenant_id = "t-2"  # type: ignore[misc]


def test_llm_usage_record_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        LLMUsageRecord(
            record_id="llmu-6",
            tenant_id="t-1",
            trace_id="trace-1",
            provider="openrouter",
            latency_ms=0,
            status=LLMUsageStatus.OK,
            created_at=_now(),
            unexpected_field="boom",  # type: ignore[call-arg]
        )
