"""End-to-end integration test for :func:`evaluate_patch` against a real
SWE-bench eval Docker image.

Pulls (if needed) and runs ``swebench/sweb.eval.<arch>.psf_1776_requests-2317``
with the dataset's gold patch and asserts the **harness-level contract**:
patch applies, no pipeline error, every selector gets a parsed verdict
(not ``missing``). Skipped unless Docker is available and the image is
locally cached — keeps unit CI fast and offline, and keeps the test from
running uncached pulls on every developer machine.

What this test does NOT assert:

* That selectors actually PASS. Every FAIL_TO_PASS in this instance
  hits ``httpbin.org``; whether they pass depends on httpbin's
  availability, the runner's DNS configuration, and ISP/VPN behaviour
  (see ``docs/specs/EVAL_BASELINE.md`` "已知环境敏感性"). The score
  number is an environment + harness + agent product — this test
  isolates the harness factor.

This is the test that would have caught the GHA gate's "all selectors
missing / test_command_exit_code=1" regressions — running it locally
during PR review is fast (the image is cached) and gives real signal.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from eval.swebench.dataset import load_instance
from eval.swebench.evaluate import evaluate_patch
from eval.swebench.images import image_name_for_instance

pytestmark = pytest.mark.integration

_INSTANCE_ID = "psf__requests-2317"


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _image_cached(image: str) -> bool:
    if not _docker_available():
        return False
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


async def test_evaluate_psf_requests_2317_harness_contract(
    tmp_path: Path,
) -> None:
    """Real-docker end-to-end: harness-level contract holds for a gold-patch run.

    Skipped unless Docker is available and the eval image is already
    cached locally — pulling the image takes 5–10 minutes and we don't
    want every test session to do that.

    The contract this asserts:

    * patch_applied: True — gold patch round-trips through git apply
    * error: None — no pipeline-level failure (image pull, container
      exec, etc.)
    * Every selector has a parsed verdict (not ``missing``) — proves
      the test runner actually executed and the parser recognised the
      output. ``missing`` would indicate the runner produced no
      output we knew how to parse, which is the failure mode the
      original GHA gate kept hitting.

    Pass/fail verdicts themselves are NOT asserted — see module
    docstring re: ``httpbin.org`` dependency.
    """

    inst = load_instance(_INSTANCE_ID)
    image = image_name_for_instance(inst)

    if not _docker_available():
        pytest.skip("docker CLI not on PATH")
    if not _image_cached(image):
        pytest.skip(
            f"eval image {image} not cached locally; "
            f"pull manually with `docker pull {image}` to enable this test"
        )

    log_path = tmp_path / "test_output.log"
    result = await evaluate_patch(inst, inst.patch, test_output_path=log_path)

    assert result.patch_applied is True, (
        f"gold patch failed to apply; error={result.error}; see test output at {log_path}"
    )
    assert result.error is None
    assert result.test_command_exit_code is not None

    # Every selector should have an actual verdict. ``missing`` here
    # would mean the runner emitted nothing the parser recognised —
    # the original GHA-gate failure mode. A pass/fail/error verdict
    # is fine (whether tests pass is environment business).
    missing = [r for r in result.fail_to_pass + result.pass_to_pass if r.status == "missing"]
    assert not missing, (
        f"runner produced no parseable verdict for {len(missing)} selector(s) "
        f"(harness or parser regression); "
        f"first few: {[r.selector for r in missing[:3]]}; "
        f"see raw test output at {log_path}"
    )

    # ``--log-test-output`` actually wrote something.
    assert log_path.exists()
    assert log_path.stat().st_size > 0
