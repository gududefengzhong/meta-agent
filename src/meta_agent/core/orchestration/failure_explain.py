"""Small helpers for user-facing failure explanations.

These projections are intentionally JSON dicts rather than pydantic
models: graph outputs are already arbitrary JSON, and we want a stable,
low-friction shape that can be embedded in ``output`` or CLI reports.
"""

from __future__ import annotations

from typing import Any, Literal

FailureCategory = Literal[
    "verifier_failed",
    "tool_failed",
    "llm_failed",
    "budget_exceeded",
    "max_steps_truncated",
    "infra_error",
]


def failure_explanation(
    *,
    category: FailureCategory,
    summary: str,
    retryable: bool,
    hints: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the stable failure-explanation payload used by outputs."""

    payload: dict[str, Any] = {
        "category": category,
        "summary": summary,
        "retryable": retryable,
        "hints": list(hints or []),
    }
    if details:
        payload["details"] = details
    return payload
