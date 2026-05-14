"""Unit tests for BillingEvent model."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from meta_agent.core.domain import BillingEvent


def _now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=UTC)


def _billing(**overrides: object) -> BillingEvent:
    base: dict[str, object] = {
        "event_id": "be-1",
        "tenant_id": "t-1",
        "trace_id": "trace-1",
        "model": "anthropic/claude-opus-4.7",
        "provider": "openrouter",
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cost": Decimal("0.0123"),
        "currency": "USD",
        "occurred_at": _now(),
    }
    base.update(overrides)
    return BillingEvent(**base)


def test_billing_accepts_zero_tokens_and_cost() -> None:
    event = _billing(prompt_tokens=0, completion_tokens=0, total_tokens=0, cost=Decimal("0"))
    assert event.cost == Decimal("0")


def test_billing_rejects_negative_tokens() -> None:
    with pytest.raises(ValidationError):
        _billing(prompt_tokens=-1)


def test_billing_rejects_negative_cost() -> None:
    with pytest.raises(ValidationError):
        _billing(cost=Decimal("-0.01"))


def test_billing_currency_is_three_letter() -> None:
    with pytest.raises(ValidationError):
        _billing(currency="US")
    with pytest.raises(ValidationError):
        _billing(currency="USDX")
