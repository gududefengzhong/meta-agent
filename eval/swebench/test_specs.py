"""Per-instance test specs: which command runs the tests + which parser interprets the output.

**Phase-1 scope (see** ``docs/specs/EVAL_BASELINE.md`` **Standard 5)**:
pytest-friendly repos only. Django / sympy / other non-pytest
runners are deliberately **not** in the whitelist — they were the
single biggest source of harness drift in the previous round
(parenthesised selectors, separate test scripts, conda env
variances). They re-enter the whitelist in a later phase once the
pytest path is proven stable end-to-end against real eval images.

Off-whitelist instances cause :class:`TestSpecNotFoundError` from
:func:`spec_for`. Callers should treat that as ``skipped`` (out of
scope), distinct from ``failed`` (in scope, tests didn't pass).

Values mirror upstream
``swebench.harness.constants.MAP_REPO_VERSION_TO_SPECS`` and
``swebench.harness.log_parsers.python.MAP_REPO_TO_PARSER_PY``
(MIT-licensed). Copying instead of importing avoids pulling
``datasets`` / ``pyarrow`` / ``docker`` SDK as transitive
dependencies; the trade-off is we have to track upstream
manually when adding repos.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from eval.swebench.instances import SWEBenchInstance


class TestSpecNotFoundError(Exception):
    """Raised when no spec is registered for an instance's (repo, version)."""

    # The class name starts with "Test" — opt out of pytest collection so it
    # isn't picked up as a candidate test class.
    __test__: ClassVar[bool] = False


@dataclass(frozen=True)
class TestSpec:
    """How to run + interpret tests for one SWE-bench instance family.

    Attributes:
        test_cmd: The shell command (run via ``bash -c``) that
            invokes the runner. Selectors are appended at run time
            after shell-quoting.
        parser: Key into :data:`eval.swebench.log_parsers.PARSER_BY_NAME`
            picking the parser that converts the runner's stdout
            into ``dict[selector, TestStatus]``.
    """

    __test__: ClassVar[bool] = False

    test_cmd: str
    parser: str


# Whitelist keyed by (repo, version). Values transcribed from upstream
# SWE-bench. When extending, prefer copying upstream verbatim
# rather than re-deriving — drift here changes pass@1.
#
# Phase-1 whitelist is pytest-only. Adding a non-pytest runner
# (Django / sympy / etc.) is a Standard 5 scope expansion: needs
# the corresponding parser in ``log_parsers.py``, real-docker
# validation against at least one gold patch, and a dedicated PR
# — not a one-line addition here.
_SPECS: dict[tuple[str, str], TestSpec] = {
    ("psf/requests", "2.4"): TestSpec(
        test_cmd="pytest -rA",
        parser="pytest_options",
    ),
}


def spec_for(instance: SWEBenchInstance) -> TestSpec:
    """Return the :class:`TestSpec` for ``instance`` or raise.

    Raises:
        TestSpecNotFoundError: ``instance.repo`` / ``instance.version``
            isn't in the registered table. Surface this to operators
            as a "harness can't evaluate this row" — not a test
            failure — so the gate report distinguishes "spec missing"
            from "tests failed".
    """

    key = (instance.repo, instance.version)
    spec = _SPECS.get(key)
    if spec is None:
        raise TestSpecNotFoundError(
            f"no test spec registered for repo={instance.repo!r} version={instance.version!r}"
        )
    return spec


__all__ = ["TestSpec", "TestSpecNotFoundError", "spec_for"]
