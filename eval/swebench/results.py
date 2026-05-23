"""Result domain types for a SWE-bench instance evaluation.

Success criterion (per upstream SWE-bench):

* Every selector in ``FAIL_TO_PASS`` must transition from ``failed``
  to ``passed`` after the agent's patch is applied.
* Every selector in ``PASS_TO_PASS`` must remain ``passed`` — the
  agent must not regress prior behaviour.

A single missing selector (the test wasn't collected at all) is
treated as a failure for that selector. Errors raised by pytest
itself before any test ran (collection failure, env error) make
the whole instance result a failure regardless of selector
status, because pytest's output for individual selectors is
unreliable in that mode.

Why custom result types instead of reusing the meta-agent
``TaskResult``: the SWE-bench harness lives outside the meta-agent
graph runtime. It produces an evaluation artefact that's
serialised straight to disk / a CI report — no checkpoint,
no audit, no LLM-usage attribution.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

TestStatus = Literal["passed", "failed", "error", "missing"]
"""Per-selector outcome.

* ``passed`` — pytest reported PASSED
* ``failed`` — pytest reported FAILED (assertion or other test
  failure)
* ``error`` — pytest reported ERROR (setup / fixture / collection
  failure for this specific selector)
* ``missing`` — pytest did not report this selector at all (e.g.
  the test was renamed away by the patch)
"""


class TestSelectorResult(BaseModel):
    """Outcome for one test selector after running pytest."""

    # pytest considers anything named ``Test*`` a candidate test
    # class; this opt-out keeps the collector quiet for what is
    # really a domain model.
    __test__: ClassVar[bool] = False

    model_config = ConfigDict(frozen=True, extra="forbid")

    selector: str = Field(..., min_length=1)
    status: TestStatus

    @property
    def passed(self) -> bool:
        return self.status == "passed"


class InstanceResult(BaseModel):
    """Aggregated outcome of evaluating one patch against one instance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instance_id: str = Field(..., min_length=1)
    image: str = Field(..., min_length=1)

    fail_to_pass: tuple[TestSelectorResult, ...] = Field(default_factory=tuple)
    pass_to_pass: tuple[TestSelectorResult, ...] = Field(default_factory=tuple)

    patch_applied: bool
    """``True`` when ``git apply`` succeeded inside the container.

    A patch that fails to apply (malformed, conflicts) is an
    automatic instance failure — the rest of the result is
    surfaced anyway so operators can diagnose.
    """

    test_command_exit_code: int | None = None
    """Exit code of the pytest invocation; ``None`` when pytest was
    never invoked (patch failed to apply, container errored before
    test run)."""

    error: str | None = None
    """Free-text error description when the pipeline aborted before
    a full result could be computed. ``None`` for clean runs."""

    @property
    def resolved(self) -> bool:
        """``True`` iff the instance passed the SWE-bench criterion.

        That is: patch applied, every FAIL_TO_PASS now passes, every
        PASS_TO_PASS still passes. Used by the CLI's exit code and
        future CI gates.
        """
        if not self.patch_applied or self.error is not None:
            return False
        if not all(r.passed for r in self.fail_to_pass):
            return False
        return all(r.passed for r in self.pass_to_pass)

    @property
    def summary(self) -> str:
        """Single-line human-readable status; goes to stderr in the CLI."""
        if not self.patch_applied:
            return f"{self.instance_id}: patch did not apply"
        if self.error is not None:
            return f"{self.instance_id}: error — {self.error}"
        f2p_pass = sum(1 for r in self.fail_to_pass if r.passed)
        p2p_pass = sum(1 for r in self.pass_to_pass if r.passed)
        verdict = "RESOLVED" if self.resolved else "FAILED"
        return (
            f"{self.instance_id}: {verdict} "
            f"(FAIL_TO_PASS {f2p_pass}/{len(self.fail_to_pass)}, "
            f"PASS_TO_PASS {p2p_pass}/{len(self.pass_to_pass)})"
        )


__all__ = ["InstanceResult", "TestSelectorResult", "TestStatus"]
