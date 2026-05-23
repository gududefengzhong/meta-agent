"""Unit tests for :mod:`eval.swebench.evaluate` with a scripted Docker layer."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest
from eval.swebench.containers import DockerError
from eval.swebench.evaluate import _parse_pytest_output, evaluate_patch
from eval.swebench.instances import SWEBenchInstance

from eval.swebench import containers, evaluate


@dataclass
class _ScriptedDocker:
    responses: list[tuple[str, subprocess.CompletedProcess[str]]] = field(default_factory=list)
    calls: list[tuple[tuple[str, ...], str | None]] = field(default_factory=list)

    def run(
        self,
        cmd: Sequence[str],
        *,
        what: str,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        cmd_tuple = tuple(cmd)
        self.calls.append((cmd_tuple, input_text))
        verb = cmd_tuple[1] if len(cmd_tuple) > 1 else ""
        for idx, (match_verb, response) in enumerate(self.responses):
            if verb == match_verb:
                del self.responses[idx]
                if check and response.returncode != 0:
                    raise DockerError(
                        f"{what} failed (exit {response.returncode}): {response.stderr}"
                    )
                return response
        # Default: success.
        return subprocess.CompletedProcess(list(cmd_tuple), 0, "", "")


@pytest.fixture
def scripted_docker(monkeypatch: pytest.MonkeyPatch) -> _ScriptedDocker:
    fake = _ScriptedDocker()
    monkeypatch.setattr(containers, "_docker_run", fake.run)
    return fake


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _instance() -> SWEBenchInstance:
    return SWEBenchInstance(
        instance_id="django__django-1",
        repo="django/django",
        base_commit="abc123",
        problem_statement="fix the thing",
        test_patch=(
            "diff --git a/tests/test_dispatch.py b/tests/test_dispatch.py\n"
            "--- a/tests/test_dispatch.py\n"
            "+++ b/tests/test_dispatch.py\n"
        ),
        fail_to_pass=("tests/test_dispatch.py::test_logs",),
        pass_to_pass=("tests/test_dispatch.py::test_other",),
    )


# ----------------------------------------------------- parser unit


def test_parse_pytest_output_recognises_three_verbs() -> None:
    output = (
        "PASSED tests/test_x.py::test_a\n"
        "FAILED tests/test_x.py::test_b - AssertionError: nope\n"
        "ERROR tests/test_x.py::test_c - fixture not found\n"
        "============ 1 passed, 1 failed, 1 error in 0.12s ============\n"
    )
    out = _parse_pytest_output(output)
    assert out == {
        "tests/test_x.py::test_a": "passed",
        "tests/test_x.py::test_b": "failed",
        "tests/test_x.py::test_c": "error",
    }


def test_parse_pytest_output_ignores_summary_lines_and_blank_lines() -> None:
    out = _parse_pytest_output("\nblahblah\n===== 5 passed in 1.2s =====\n")
    assert out == {}


def test_parse_pytest_output_later_lines_win_on_duplicate_selector() -> None:
    output = "PASSED tests/test_x.py::test_a\nFAILED tests/test_x.py::test_a - AssertionError\n"
    out = _parse_pytest_output(output)
    assert out == {"tests/test_x.py::test_a": "failed"}


# ----------------------------------------------------- evaluate_patch


async def test_evaluate_resolved_when_all_selectors_pass(
    scripted_docker: _ScriptedDocker,
) -> None:
    pytest_stdout = (
        "PASSED tests/test_dispatch.py::test_logs\nPASSED tests/test_dispatch.py::test_other\n"
    )
    scripted_docker.responses.extend(
        [
            ("image", _completed(returncode=0)),  # image cached
            ("run", _completed(returncode=0)),  # docker run -d
            ("exec", _completed(returncode=0)),  # apply test_patch
            ("exec", _completed(returncode=0)),  # apply agent patch
            ("exec", _completed(returncode=0, stdout=pytest_stdout)),  # pytest
            ("rm", _completed(returncode=0)),  # teardown
        ]
    )
    result = await evaluate_patch(_instance(), "diff --git a/x b/x\n")
    assert result.resolved is True
    assert result.patch_applied is True
    assert all(r.passed for r in result.fail_to_pass)
    assert all(r.passed for r in result.pass_to_pass)


async def test_evaluate_not_resolved_when_fail_to_pass_still_fails(
    scripted_docker: _ScriptedDocker,
) -> None:
    pytest_stdout = (
        "FAILED tests/test_dispatch.py::test_logs - AssertionError\n"
        "PASSED tests/test_dispatch.py::test_other\n"
    )
    scripted_docker.responses.extend(
        [
            ("image", _completed(returncode=0)),
            ("run", _completed(returncode=0)),
            ("exec", _completed(returncode=0)),  # test_patch apply
            ("exec", _completed(returncode=0)),  # agent patch apply
            ("exec", _completed(returncode=1, stdout=pytest_stdout)),
            ("rm", _completed(returncode=0)),
        ]
    )
    result = await evaluate_patch(_instance(), "diff --git a/x b/x\n")
    assert result.resolved is False
    assert result.fail_to_pass[0].status == "failed"
    assert result.pass_to_pass[0].passed is True


async def test_evaluate_missing_selector_treated_as_failure(
    scripted_docker: _ScriptedDocker,
) -> None:
    # pytest reports only one of the two selectors (perhaps the
    # FAIL_TO_PASS test was renamed by a buggy patch).
    pytest_stdout = "PASSED tests/test_dispatch.py::test_other\n"
    scripted_docker.responses.extend(
        [
            ("image", _completed(returncode=0)),
            ("run", _completed(returncode=0)),
            ("exec", _completed(returncode=0)),
            ("exec", _completed(returncode=0)),
            ("exec", _completed(returncode=1, stdout=pytest_stdout)),
            ("rm", _completed(returncode=0)),
        ]
    )
    result = await evaluate_patch(_instance(), "diff --git a/x b/x\n")
    assert result.fail_to_pass[0].status == "missing"
    assert result.resolved is False


async def test_evaluate_patch_apply_failure_short_circuits(
    scripted_docker: _ScriptedDocker,
) -> None:
    scripted_docker.responses.extend(
        [
            ("image", _completed(returncode=0)),
            ("run", _completed(returncode=0)),
            ("exec", _completed(returncode=0)),  # test_patch ok
            ("exec", _completed(returncode=1, stderr="patch fragment conflicts")),
            # ``rm`` is still issued by Container.__aexit__ — provide a stub
            # so the scripted fake doesn't fall through to default success
            # for the implicit teardown.
            ("rm", _completed(returncode=0)),
        ]
    )
    result = await evaluate_patch(_instance(), "diff --git a/x b/x\n")
    assert result.patch_applied is False
    assert result.error is not None
    assert "agent patch apply failed" in result.error
    assert result.resolved is False
    # Pytest was never invoked.
    verbs = [c[0][1] for c in scripted_docker.calls]
    assert "exec" not in verbs[verbs.index("exec") + 2 :]


async def test_evaluate_image_pull_failure_reports_structured_error(
    scripted_docker: _ScriptedDocker,
) -> None:
    scripted_docker.responses.extend(
        [
            ("image", _completed(returncode=1, stderr="No such image")),
            ("pull", _completed(returncode=1, stderr="rate limited")),
        ]
    )
    result = await evaluate_patch(_instance(), "diff\n")
    assert result.patch_applied is False
    assert result.error is not None
    assert "image pull failed" in result.error
    assert result.resolved is False


async def test_evaluate_empty_patch_still_runs_tests(
    scripted_docker: _ScriptedDocker,
) -> None:
    """Empty patch is legal — the test_patch alone may already pass FAIL_TO_PASS."""

    pytest_stdout = (
        "FAILED tests/test_dispatch.py::test_logs - AssertionError\n"
        "PASSED tests/test_dispatch.py::test_other\n"
    )
    scripted_docker.responses.extend(
        [
            ("image", _completed(returncode=0)),
            ("run", _completed(returncode=0)),
            ("exec", _completed(returncode=0)),  # test_patch apply
            # No agent patch apply call — patch_text is "".
            ("exec", _completed(returncode=1, stdout=pytest_stdout)),  # pytest
            ("rm", _completed(returncode=0)),
        ]
    )
    result = await evaluate_patch(_instance(), "")
    assert result.patch_applied is True
    # FAIL_TO_PASS still failing means not resolved.
    assert result.resolved is False
    # Make sure we didn't try to apply an empty patch.
    exec_calls = [c for c in scripted_docker.calls if c[0][1] == "exec"]
    git_apply_calls = [c for c in exec_calls if "git" in c[0] and "apply" in c[0]]
    # Only one git apply (the test_patch), not two.
    assert len(git_apply_calls) == 1


async def test_evaluate_instance_with_no_selectors_resolves_vacuously(
    scripted_docker: _ScriptedDocker,
) -> None:
    inst = SWEBenchInstance(
        instance_id="x__y-1",
        repo="x/y",
        base_commit="abc",
    )
    scripted_docker.responses.extend(
        [
            ("image", _completed(returncode=0)),
            ("run", _completed(returncode=0)),
            ("rm", _completed(returncode=0)),
        ]
    )
    result = await evaluate_patch(inst, "")
    assert result.resolved is True
    assert result.fail_to_pass == ()
    assert result.pass_to_pass == ()


# Module-level reference so the unused-import guard doesn't complain about
# ``evaluate`` (we monkeypatch its dep, not the module itself).
_ = evaluate
