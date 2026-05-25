"""Per-runner log parsers for SWE-bench test output.

Phase-1 scope per ``docs/specs/EVAL_BASELINE.md`` Standard 5:
**pytest only**. Django's unittest runner and sympy's ``bin/test``
were removed when the harness was rebuilt — they re-enter the
whitelist in a later phase after the pytest path is proven stable
against real eval images.

Logic for the remaining two parsers is ported from upstream
``swebench.harness.log_parsers.python`` (MIT licensed). Kept
intentionally close to upstream so our scoring doesn't silently
diverge.

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
}


__all__ = [
    "PARSER_BY_NAME",
    "LogParser",
    "parse_pytest",
    "parse_pytest_options",
]
