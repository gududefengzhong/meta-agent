"""Per-runner log parsers for SWE-bench test output.

SWE-bench instances span repos that use different test runners
(Django's ``unittest`` runner, sympy's ``bin/test``, plain
pytest, pytest with options, …). Each runner emits results in a
different format, so parsing has to be runner-specific.

The four parsers here cover the runners our built-in fixture
needs. Logic is ported from upstream
``swebench.harness.log_parsers.python`` (MIT licensed) and kept
intentionally close to upstream — drift between our parsing and
upstream's reference scoring would silently change pass@1 and
make our numbers incomparable with published baselines.

We do not depend on the ``swebench`` PyPI package as a runtime
dep because that package pulls in ``datasets``, ``pyarrow``,
``docker`` SDK and a few hundred MB of transitive deps just for
what's effectively a constants table here. Copy + attribute +
unit-test instead.

Each parser returns ``dict[selector, TestStatus]``; missing
selectors land as ``"missing"`` at the scoring layer.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from eval.swebench.results import TestStatus

LogParser = Callable[[str], dict[str, TestStatus]]


def parse_pytest(log: str) -> dict[str, TestStatus]:
    """Parse plain ``pytest -v`` output.

    Recognises lines like ``PASSED tests/x.py::test_y``. The verb
    is the first whitespace-separated token; the selector is the
    second. Trailing failure context (``- AssertionError: ...``)
    is stripped before splitting.
    """

    out: dict[str, TestStatus] = {}
    for line in log.split("\n"):
        verb = _verb_prefix(line)
        if verb is None:
            continue
        cleaned = line.replace(" - ", " ", 1) if verb == "FAILED" else line
        parts = cleaned.split()
        if len(parts) < 2:
            continue
        status = _STATUS_BY_VERB.get(parts[0])
        if status is None:
            continue
        out[parts[1]] = status
    return out


def parse_pytest_options(log: str) -> dict[str, TestStatus]:
    """``pytest -rA`` output, with parametrised test name normalisation.

    Parametrised tests print as ``module::test[param]``. Some
    images path-rewrite the parameter; upstream collapses long
    file-path parameters to their basename so the selector
    matches the dataset's recorded form.
    """

    option_pattern = re.compile(r"(.*?)\[(.*)\]")
    out: dict[str, TestStatus] = {}
    for line in log.split("\n"):
        verb = _verb_prefix(line)
        if verb is None:
            continue
        cleaned = line.replace(" - ", " ", 1) if verb == "FAILED" else line
        parts = cleaned.split()
        if len(parts) < 2:
            continue
        status = _STATUS_BY_VERB.get(parts[0])
        if status is None:
            continue
        test_token = parts[1]
        match = option_pattern.search(test_token)
        if match:
            main, option = match.groups()
            if option.startswith("/") and not option.startswith("//") and "*" not in option:
                option = "/" + option.split("/")[-1]
            test_token = f"{main}[{option}]"
        out[test_token] = status
    return out


def parse_django(log: str) -> dict[str, TestStatus]:
    """Parse Django's unittest runner output.

    Django prints one line per test of the form ::

        test_name (dotted.module.ClassName) ... ok
        test_name (dotted.module.ClassName) ... FAIL
        test_name (dotted.module.ClassName) ... ERROR

    Plus a ``FAIL: test_name (dotted.module.ClassName)`` /
    ``ERROR: ...`` prefix in the summary. The selector format
    SWE-bench records matches what the runner emits, so no
    translation is needed at the parser layer.
    """

    out: dict[str, TestStatus] = {}
    prev_test: str | None = None
    for raw_line in log.split("\n"):
        line = raw_line.strip()
        if " ... " in line:
            prev_test = line.split(" ... ")[0]
        for suffix in (" ... ok", " ... OK", " ...  OK"):
            if line.endswith(suffix):
                out[line.rsplit(suffix, 1)[0]] = "passed"
                break
        if " ... skipped" in line:
            out[line.split(" ... skipped")[0]] = "passed"  # skipped → not a failure
        if line.endswith(" ... FAIL"):
            out[line.split(" ... FAIL")[0]] = "failed"
        if line.startswith("FAIL:"):
            parts = line.split(maxsplit=1)
            if len(parts) > 1:
                out[parts[1].strip()] = "failed"
        if line.endswith(" ... ERROR"):
            out[line.split(" ... ERROR")[0]] = "error"
        if line.startswith("ERROR:"):
            parts = line.split(maxsplit=1)
            if len(parts) > 1:
                out[parts[1].strip()] = "error"
        if line.lstrip().startswith("ok") and prev_test is not None:
            # Some long-running tests print intervening output
            # between "..." and the trailing "ok"; pair them by
            # remembering the last "... " line.
            out[prev_test] = "passed"
            prev_test = None
    return out


def parse_sympy(log: str) -> dict[str, TestStatus]:
    """Parse sympy ``bin/test`` output.

    Sympy emits lines like ``test_foo F`` / ``test_foo E`` /
    ``test_foo ok`` with bare test names. A separate failure
    summary repeats the file path + function name; we record
    those as failures too so they're visible to the scorer.
    """

    out: dict[str, TestStatus] = {}
    summary_re = re.compile(r"(_*) (.*)\.py:(.*) (_*)")
    for match in summary_re.findall(log):
        out[f"{match[1]}.py:{match[2]}"] = "failed"
    for raw_line in log.split("\n"):
        line = raw_line.strip()
        if not line.startswith("test_"):
            continue
        if line.endswith(" E"):
            out[line.split()[0]] = "error"
        elif line.endswith(" F"):
            out[line.split()[0]] = "failed"
        elif line.endswith(" ok"):
            out[line.split()[0]] = "passed"
    return out


_STATUS_BY_VERB: dict[str, TestStatus] = {
    "PASSED": "passed",
    "FAILED": "failed",
    "ERROR": "error",
    "SKIPPED": "passed",  # skipped tests don't count as failure for SWE-bench
}


def _verb_prefix(line: str) -> str | None:
    """Return the leading verb if ``line`` starts with one we recognise."""

    for verb in _STATUS_BY_VERB:
        if line.startswith(verb):
            return verb
    return None


PARSER_BY_NAME: dict[str, LogParser] = {
    "pytest": parse_pytest,
    "pytest_options": parse_pytest_options,
    "django": parse_django,
    "sympy": parse_sympy,
}


__all__ = [
    "PARSER_BY_NAME",
    "LogParser",
    "parse_django",
    "parse_pytest",
    "parse_pytest_options",
    "parse_sympy",
]
