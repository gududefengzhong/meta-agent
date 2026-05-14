"""Unit tests for AgentError and ErrorCategory."""

from __future__ import annotations

import pytest

from meta_agent.core.domain import AgentError, ErrorCategory


def test_default_category_is_logic_and_not_retryable() -> None:
    err = AgentError("boom")
    assert err.category is ErrorCategory.LOGIC
    assert err.retryable is False


def test_transient_category_is_retryable() -> None:
    err = AgentError("upstream slow", category=ErrorCategory.TRANSIENT)
    assert err.category is ErrorCategory.TRANSIENT
    assert err.retryable is True


@pytest.mark.parametrize(
    "category",
    [
        ErrorCategory.EXTERNAL,
        ErrorCategory.PERMISSION,
        ErrorCategory.VALIDATION,
        ErrorCategory.LOGIC,
        ErrorCategory.USER_CANCELLED,
    ],
)
def test_non_transient_categories_are_not_retryable(category: ErrorCategory) -> None:
    err = AgentError("x", category=category)
    assert err.retryable is False


def test_subclass_can_set_default_category() -> None:
    class PermissionDeniedError(AgentError):
        category = ErrorCategory.PERMISSION

    err = PermissionDeniedError("nope")
    assert err.category is ErrorCategory.PERMISSION
    assert err.retryable is False
