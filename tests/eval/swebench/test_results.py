"""Unit tests for :class:`InstanceResult` / :class:`TestSelectorResult`."""

from __future__ import annotations

from eval.swebench.results import InstanceResult, TestSelectorResult, TestStatus


def _passing(selector: str) -> TestSelectorResult:
    return TestSelectorResult(selector=selector, status="passed")


def _failing(selector: str, status: TestStatus = "failed") -> TestSelectorResult:
    return TestSelectorResult(selector=selector, status=status)


def test_resolved_when_all_selectors_pass() -> None:
    result = InstanceResult(
        instance_id="x__y-1",
        image="img:latest",
        fail_to_pass=(_passing("a"), _passing("b")),
        pass_to_pass=(_passing("c"),),
        patch_applied=True,
        test_command_exit_code=0,
    )
    assert result.resolved is True


def test_not_resolved_when_one_fail_to_pass_still_fails() -> None:
    result = InstanceResult(
        instance_id="x__y-1",
        image="img:latest",
        fail_to_pass=(_passing("a"), _failing("b")),
        pass_to_pass=(_passing("c"),),
        patch_applied=True,
        test_command_exit_code=1,
    )
    assert result.resolved is False


def test_not_resolved_when_pass_to_pass_regresses() -> None:
    result = InstanceResult(
        instance_id="x__y-1",
        image="img:latest",
        fail_to_pass=(_passing("a"),),
        pass_to_pass=(_failing("c"),),
        patch_applied=True,
        test_command_exit_code=1,
    )
    assert result.resolved is False


def test_not_resolved_when_selector_is_missing() -> None:
    result = InstanceResult(
        instance_id="x__y-1",
        image="img:latest",
        fail_to_pass=(_failing("a", status="missing"),),
        pass_to_pass=(),
        patch_applied=True,
        test_command_exit_code=0,
    )
    assert result.resolved is False


def test_not_resolved_when_patch_did_not_apply() -> None:
    result = InstanceResult(
        instance_id="x__y-1",
        image="img:latest",
        patch_applied=False,
        error="conflict",
    )
    assert result.resolved is False


def test_not_resolved_when_error_set_even_with_passing_selectors() -> None:
    result = InstanceResult(
        instance_id="x__y-1",
        image="img:latest",
        fail_to_pass=(_passing("a"),),
        pass_to_pass=(_passing("c"),),
        patch_applied=True,
        test_command_exit_code=0,
        error="pytest collection failed",
    )
    assert result.resolved is False


def test_summary_carries_pass_counts() -> None:
    result = InstanceResult(
        instance_id="x__y-1",
        image="img:latest",
        fail_to_pass=(_passing("a"), _failing("b")),
        pass_to_pass=(_passing("c"), _passing("d")),
        patch_applied=True,
        test_command_exit_code=1,
    )
    text = result.summary
    assert "x__y-1" in text
    assert "FAIL_TO_PASS 1/2" in text
    assert "PASS_TO_PASS 2/2" in text
    assert "FAILED" in text


def test_summary_for_patch_apply_failure() -> None:
    result = InstanceResult(
        instance_id="x__y-1",
        image="img:latest",
        patch_applied=False,
    )
    assert "patch did not apply" in result.summary


def test_selector_passed_property() -> None:
    assert _passing("a").passed is True
    assert _failing("a").passed is False
    assert _failing("a", status="error").passed is False
    assert _failing("a", status="missing").passed is False
