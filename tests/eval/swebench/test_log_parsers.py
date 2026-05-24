"""Unit tests for :mod:`eval.swebench.log_parsers`.

These cover each runner's output format with realistic snippets
captured from real eval-image runs (lightly trimmed). Drift here
means our scoring disagrees with upstream SWE-bench, so changes
to these parsers should be cross-checked against upstream.
"""

from __future__ import annotations

from eval.swebench.log_parsers import (
    PARSER_BY_NAME,
    parse_django,
    parse_pytest,
    parse_pytest_options,
    parse_sympy,
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


# ----------------------------------------------------------------- parse_django


def test_parse_django_passes_and_failures_and_errors() -> None:
    log = (
        "test_send_robust_fail (dispatch.tests.DispatcherTests) ... ok\n"
        "test_send_different_no_sender (dispatch.tests.DispatcherTests) ... FAIL\n"
        "test_uid_registration (dispatch.tests.DispatcherTests) ... ERROR\n"
        "test_send_robust_no_receivers (dispatch.tests.DispatcherTests) ... ok\n"
    )
    out = parse_django(log)
    assert out == {
        "test_send_robust_fail (dispatch.tests.DispatcherTests)": "passed",
        "test_send_different_no_sender (dispatch.tests.DispatcherTests)": "failed",
        "test_uid_registration (dispatch.tests.DispatcherTests)": "error",
        "test_send_robust_no_receivers (dispatch.tests.DispatcherTests)": "passed",
    }


def test_parse_django_summary_FAIL_and_ERROR_prefixes_also_recognised() -> None:
    log = "FAIL: test_x (mod.MyTests)\nERROR: test_y (mod.MyTests)\n"
    out = parse_django(log)
    assert out["test_x (mod.MyTests)"] == "failed"
    assert out["test_y (mod.MyTests)"] == "error"


def test_parse_django_handles_intervening_output_before_ok() -> None:
    # Some long-running Django tests print other output on the
    # same line as ``...``, then ``ok`` on the next line.
    log = "test_settings_check (mod.MyTests) ... System check identified no issues.\nok\n"
    out = parse_django(log)
    assert out["test_settings_check (mod.MyTests)"] == "passed"


def test_parse_django_skipped_counts_as_passed() -> None:
    log = "test_skipped (mod.MyTests) ... skipped 'reason'\n"
    out = parse_django(log)
    assert out["test_skipped (mod.MyTests)"] == "passed"


# ----------------------------------------------------------------- parse_sympy


def test_parse_sympy_recognises_inline_verdicts() -> None:
    log = "test_immutable ok\ntest_Symbol ok\ntest_symbol_bug F\ntest_other E\n"
    out = parse_sympy(log)
    assert out == {
        "test_immutable": "passed",
        "test_Symbol": "passed",
        "test_symbol_bug": "failed",
        "test_other": "error",
    }


def test_parse_sympy_picks_up_failure_summary_blocks() -> None:
    log = (
        "________________ sympy/core/tests/test_symbol.py:test_bug ________________\n"
        "AssertionError\n"
    )
    out = parse_sympy(log)
    # The summary block records the test as failed.
    assert "sympy/core/tests/test_symbol.py:test_bug" in out
    assert out["sympy/core/tests/test_symbol.py:test_bug"] == "failed"


# ----------------------------------------------------------------- registry


def test_parser_by_name_registry_covers_each_parser() -> None:
    assert PARSER_BY_NAME["pytest"] is parse_pytest
    assert PARSER_BY_NAME["pytest_options"] is parse_pytest_options
    assert PARSER_BY_NAME["django"] is parse_django
    assert PARSER_BY_NAME["sympy"] is parse_sympy
