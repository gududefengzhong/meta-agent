"""Unit tests for :mod:`eval.swebench.log_parsers`.

Phase-1 scope: pytest only. Django + sympy parsers are out of
this phase's whitelist (see ``docs/specs/EVAL_BASELINE.md``
Standard 5) and the tests for them are intentionally absent.
They come back when the corresponding parsers come back.
"""

from __future__ import annotations

from eval.swebench.log_parsers import (
    PARSER_BY_NAME,
    parse_pytest,
    parse_pytest_options,
)

# ----------------------------------------------------------------- parse_pytest


def test_parse_pytest_recognises_passed_failed_error() -> None:
    log = (
        "PASSED tests/test_x.py::test_a\n"
        "FAILED tests/test_x.py::test_b - AssertionError: nope\n"
        "ERROR tests/test_x.py::test_c - fixture not found\n"
        "============ 1 passed, 1 failed, 1 error in 0.12s ============\n"
    )
    assert parse_pytest(log) == {
        "tests/test_x.py::test_a": "passed",
        "tests/test_x.py::test_b": "failed",
        "tests/test_x.py::test_c": "error",
    }


def test_parse_pytest_skipped_counts_as_passed() -> None:
    # Skipped tests don't count as a failure under SWE-bench's
    # criterion — only outright failures regress.
    log = "SKIPPED tests/test_x.py::test_skip - py.skip()\n"
    assert parse_pytest(log) == {"tests/test_x.py::test_skip": "passed"}


def test_parse_pytest_ignores_blank_and_summary_lines() -> None:
    assert parse_pytest("\nblah\n===== 5 passed in 1.2s =====\n") == {}


def test_parse_pytest_later_line_wins_on_duplicate() -> None:
    log = "PASSED tests/test_x.py::test_a\nFAILED tests/test_x.py::test_a - AssertionError\n"
    assert parse_pytest(log) == {"tests/test_x.py::test_a": "failed"}


# --------------------------------------------------------- parse_pytest_options


def test_parse_pytest_options_handles_plain_selectors() -> None:
    log = "PASSED test_requests.py::TestRequests::test_HTTP_302_ALLOW_REDIRECT_GET\n"
    assert parse_pytest_options(log) == {
        "test_requests.py::TestRequests::test_HTTP_302_ALLOW_REDIRECT_GET": "passed",
    }


def test_parse_pytest_options_collapses_long_path_parameter() -> None:
    # When the parameter is a path like ``/var/tmp/something/foo.py``,
    # upstream collapses it to ``/foo.py`` so the selector matches
    # the dataset's recorded form.
    log = "FAILED tests/test_x.py::test_param[/tmp/with/long/path/foo.py] - oops\n"
    out = parse_pytest_options(log)
    assert out == {"tests/test_x.py::test_param[/foo.py]": "failed"}


def test_parse_pytest_options_leaves_short_parameter_untouched() -> None:
    log = "PASSED tests/test_x.py::test_param[small]\n"
    assert parse_pytest_options(log) == {"tests/test_x.py::test_param[small]": "passed"}


# ----------------------------------------------------------------- registry


def test_parser_by_name_registry_only_exposes_pytest_parsers_in_phase_1() -> None:
    # Standard 5: Django / sympy parsers are deliberately not
    # in the whitelist this phase. Guard so they aren't added
    # back without a corresponding spec + integration test.
    assert set(PARSER_BY_NAME.keys()) == {"pytest", "pytest_options"}
    assert PARSER_BY_NAME["pytest"] is parse_pytest
    assert PARSER_BY_NAME["pytest_options"] is parse_pytest_options
