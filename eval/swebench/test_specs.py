"""Per-instance test specs: which command runs the tests + which parser interprets the output.

SWE-bench repos disagree on how to run tests:

* ``django/django`` uses Django's unittest runner
  (``./tests/runtests.py``) which accepts selectors of the form
  ``test_name (dotted.module.Class)``.
* ``sympy/sympy`` uses its own ``bin/test`` runner.
* Everyone else mostly uses pytest, with minor flag variation.

Feeding Django selectors to raw pytest (the harness's previous
behaviour) silently fails: pytest treats the parenthesised part
as a separate argument and reports zero tests collected, so
every selector lands as ``missing``. The :class:`TestSpec` here
encodes the right command + the right parser for each
``(repo, version)`` so the eval step actually runs.

The table is intentionally narrow — only the three repos our
built-in fixture exercises. Adding a repo means adding a row
here (test command + parser name) and a corresponding parser in
:mod:`eval.swebench.log_parsers` if one doesn't exist yet.

Values mirror upstream
``swebench.harness.constants.MAP_REPO_VERSION_TO_SPECS`` and
``swebench.harness.log_parsers.python.MAP_REPO_TO_PARSER_PY``
(MIT-licensed). Copying instead of importing avoids pulling
``datasets`` / ``pyarrow`` / ``docker`` SDK as transitive
dependencies; the trade-off is we have to manually track upstream
when adding repos.
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


# Map keyed by (repo, version). Values transcribed from upstream
# SWE-bench. When extending, prefer copying upstream verbatim
# rather than re-deriving — drift here changes pass@1.
_SPECS: dict[tuple[str, str], TestSpec] = {
    ("django/django", "3.2"): TestSpec(
        test_cmd="./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1",
        parser="django",
    ),
    ("sympy/sympy", "1.8"): TestSpec(
        test_cmd=(
            "PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose"
        ),
        parser="sympy",
    ),
    ("psf/requests", "2.5"): TestSpec(
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
