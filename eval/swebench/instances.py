"""Domain model for one row of the SWE-bench dataset.

Field shape mirrors the SWE-bench HuggingFace dataset
(``princeton-nlp/SWE-bench`` / ``SWE-bench_Lite``). Only the
fields the harness actually consumes are typed; the dataset has a
handful of extra columns (``hints_text``, ``created_at``) we
preserve in :attr:`extra` but don't model strictly.

Frozen so the loader hands the same instance to multiple consumers
(image resolver, agent submitter, patch validator) without
defensive copies.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SWEBenchInstance(BaseModel):
    """One SWE-bench task: an issue + repo state + acceptance criteria."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instance_id: str = Field(..., min_length=1)
    """Unique identifier, e.g. ``"django__django-13768"``.

    Convention: ``{org}__{repo}-{pr_number}``. Used to look up the
    per-instance Docker image and as the primary key in result
    reports.
    """

    repo: str = Field(..., min_length=1)
    """GitHub ``org/repo`` slug. Matches the issue's source repo."""

    base_commit: str = Field(..., min_length=1)
    """Commit SHA the agent starts from. The eval Docker image has
    the repo pre-checked-out at this SHA."""

    problem_statement: str = Field(default="", description="The issue text shown to the agent.")

    patch: str = Field(
        default="",
        description="Gold patch (the human-written fix). Reference only — the agent never sees this.",
    )
    test_patch: str = Field(
        default="",
        description="Patch applied to the test files to surface FAIL_TO_PASS tests. Applied separately by the harness, not by the agent.",
    )

    fail_to_pass: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Test selectors that fail BEFORE the patch and must pass AFTER. Primary success signal.",
    )
    pass_to_pass: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Test selectors that pass before the patch and must still pass after — guards against regression.",
    )

    version: str = Field(
        default="",
        description='Repo version label (e.g. ``"3.1"`` for django). Used for env selection.',
    )
    environment_setup_commit: str = Field(
        default="",
        description="Commit used for environment / dependency setup. May equal base_commit but sometimes points to a different version-pinning commit.",
    )

    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Anything the dataset includes that the harness doesn't model strictly (hints, timestamps). Preserved verbatim so downstream tooling can mine it.",
    )


__all__ = ["SWEBenchInstance"]
