"""Unit tests for :mod:`eval.swebench.evaluate` with a scripted Docker layer.

These exercise the spec-driven evaluation path: the runner
command + parser are picked from
:mod:`eval.swebench.test_specs`. The scripted Docker fake
records every exec so each test can assert on the actual shell
command we sent into the container.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest
from eval.swebench.containers import DockerError
from eval.swebench.evaluate import evaluate_patch
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


def _requests_instance() -> SWEBenchInstance:
    """A ``psf/requests`` v2.4 instance — exercises the pytest-options spec.

    psf/requests v2.4 is the sole entry in the Phase-1 whitelist
    (see ``docs/specs/EVAL_BASELINE.md`` Standard 5). Django /
    sympy / other-runner instances are intentionally out of
    scope this phase — tests for them re-enter when those specs
    re-enter.
    """

    return SWEBenchInstance(
        instance_id="psf__requests-1",
        repo="psf/requests",
        base_commit="abc123",
        problem_statement="fix the thing",
        test_patch=(
            "diff --git a/test_requests.py b/test_requests.py\n"
            "--- a/test_requests.py\n"
            "+++ b/test_requests.py\n"
        ),
        fail_to_pass=("test_requests.py::TestRequests::test_a",),
        pass_to_pass=("test_requests.py::TestRequests::test_b",),
        version="2.4",
    )


# --------------------------------------------------- pytest-options happy path


async def test_evaluate_resolved_when_all_selectors_pass(
    scripted_docker: _ScriptedDocker,
) -> None:
    pytest_stdout = (
        "PASSED test_requests.py::TestRequests::test_a\n"
        "PASSED test_requests.py::TestRequests::test_b\n"
    )
    scripted_docker.responses.extend(
        [
            ("image", _completed(returncode=0)),
            ("run", _completed(returncode=0)),
            ("exec", _completed(returncode=0)),  # test_patch apply
            ("exec", _completed(returncode=0)),  # agent patch apply
            ("exec", _completed(returncode=0, stdout=pytest_stdout)),  # pytest
            ("rm", _completed(returncode=0)),
        ]
    )
    result = await evaluate_patch(_requests_instance(), "diff --git a/x b/x\n")
    assert result.resolved is True
    assert result.patch_applied is True
    assert all(r.passed for r in result.fail_to_pass)
    assert all(r.passed for r in result.pass_to_pass)


async def test_evaluate_uses_spec_test_cmd_inside_bash_lc_with_conda_activate(
    scripted_docker: _ScriptedDocker,
) -> None:
    """The runner invocation must source the eval-image conda env."""

    scripted_docker.responses.extend(
        [
            ("image", _completed(returncode=0)),
            ("run", _completed(returncode=0)),
            ("exec", _completed(returncode=0)),  # test_patch
            ("exec", _completed(returncode=0)),  # agent patch
            (
                "exec",
                _completed(
                    returncode=0,
                    stdout="PASSED test_requests.py::TestRequests::test_a\nPASSED test_requests.py::TestRequests::test_b\n",
                ),
            ),
            ("rm", _completed(returncode=0)),
        ]
    )
    await evaluate_patch(_requests_instance(), "diff --git a/x b/x\n")
    test_exec = scripted_docker.calls[-2][0]
    assert "bash" in test_exec
    assert "-lc" in test_exec
    shell_cmd = test_exec[-1]
    assert "activate testbed" in shell_cmd
    assert "pytest -rA" in shell_cmd
    assert "test_requests.py::TestRequests::test_a" in shell_cmd


async def test_evaluate_not_resolved_when_fail_to_pass_still_fails(
    scripted_docker: _ScriptedDocker,
) -> None:
    pytest_stdout = (
        "FAILED test_requests.py::TestRequests::test_a - AssertionError\n"
        "PASSED test_requests.py::TestRequests::test_b\n"
    )
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
    result = await evaluate_patch(_requests_instance(), "diff --git a/x b/x\n")
    assert result.resolved is False
    assert result.fail_to_pass[0].status == "failed"
    assert result.pass_to_pass[0].passed is True


async def test_evaluate_missing_selector_treated_as_failure(
    scripted_docker: _ScriptedDocker,
) -> None:
    pytest_stdout = "PASSED test_requests.py::TestRequests::test_b\n"
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
    result = await evaluate_patch(_requests_instance(), "diff --git a/x b/x\n")
    assert result.fail_to_pass[0].status == "missing"
    assert result.resolved is False


# --------------------------------------------------- error paths


async def test_evaluate_unknown_repo_short_circuits_with_typed_error(
    scripted_docker: _ScriptedDocker,
) -> None:
    """An instance whose (repo, version) isn't in the spec table fails clean."""

    inst = SWEBenchInstance(
        instance_id="unknown__repo-1",
        repo="unknown/repo",
        base_commit="abc",
        fail_to_pass=("test_a",),
        version="1.0",
    )
    scripted_docker.responses.extend(
        [
            ("image", _completed(returncode=0)),
            ("run", _completed(returncode=0)),
            # No test command runs.
            ("rm", _completed(returncode=0)),
        ]
    )
    result = await evaluate_patch(inst, "diff\n")
    assert result.error is not None
    assert "no test spec registered" in result.error
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
            ("rm", _completed(returncode=0)),
        ]
    )
    result = await evaluate_patch(_requests_instance(), "diff --git a/x b/x\n")
    assert result.patch_applied is False
    assert result.error is not None
    assert "agent patch apply failed" in result.error
    assert result.resolved is False


async def test_evaluate_image_pull_failure_reports_structured_error(
    scripted_docker: _ScriptedDocker,
) -> None:
    scripted_docker.responses.extend(
        [
            ("image", _completed(returncode=1, stderr="No such image")),
            ("pull", _completed(returncode=1, stderr="rate limited")),
        ]
    )
    result = await evaluate_patch(_requests_instance(), "diff\n")
    assert result.patch_applied is False
    assert result.error is not None
    assert "image pull failed" in result.error
    assert result.resolved is False


async def test_evaluate_empty_patch_still_runs_tests(
    scripted_docker: _ScriptedDocker,
) -> None:
    """Empty patch is legal — the test_patch alone may already pass FAIL_TO_PASS."""

    pytest_stdout = (
        "FAILED test_requests.py::TestRequests::test_a - AssertionError\n"
        "PASSED test_requests.py::TestRequests::test_b\n"
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
    result = await evaluate_patch(_requests_instance(), "")
    assert result.patch_applied is True
    assert result.resolved is False
    # Confirm only one git apply (the test_patch), not two.
    exec_calls = [c for c in scripted_docker.calls if c[0][1] == "exec"]
    git_apply_calls = [c for c in exec_calls if "git" in c[0] and "apply" in c[0]]
    assert len(git_apply_calls) == 1


async def test_evaluate_instance_with_no_selectors_resolves_vacuously(
    scripted_docker: _ScriptedDocker,
) -> None:
    inst = SWEBenchInstance(
        instance_id="x__y-1",
        repo="x/y",
        base_commit="abc",
        version="1.0",
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


_ = evaluate
