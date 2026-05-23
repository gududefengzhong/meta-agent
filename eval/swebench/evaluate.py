"""Score one SWE-bench instance against an agent-produced patch (PR 3).

The orchestrator pulls the eval image, spins up a container,
applies the patch + the instance's ``test_patch`` (the second is
what surfaces the FAIL_TO_PASS selectors), runs every selector
through pytest, and parses the output into an :class:`InstanceResult`.

The shape is "patch-in, result-out" so any agent — meta-agent,
human, or another harness — can feed in a candidate patch and get
back a stable comparable result. Wiring meta-agent's CLI output
into this is a one-line orchestration on top.

Pytest output parsing
=====================
pytest's terse summary uses the format::

    PASSED tests/test_x.py::test_a
    FAILED tests/test_x.py::test_b - AssertionError
    ERROR tests/test_x.py::test_c - Fixture 'c' not found

We parse exactly those three verbs and ignore everything else.
Selectors the run didn't surface at all land as ``missing`` so
the SWE-bench criterion treats them as failed without the
parser pretending they passed.
"""

from __future__ import annotations

import logging
import re

from eval.swebench.containers import Container, DockerError, ensure_image_pulled
from eval.swebench.images import image_name_for_instance
from eval.swebench.instances import SWEBenchInstance
from eval.swebench.results import (
    InstanceResult,
    TestSelectorResult,
    TestStatus,
)

logger = logging.getLogger(__name__)

_PYTEST_LINE_RE = re.compile(r"^(?P<verb>PASSED|FAILED|ERROR)\s+(?P<selector>\S+)")
"""Match pytest's `-v` per-test status lines.

Trailing failure context (``- AssertionError: ...``) is ignored
because we only care about the verdict per selector. The regex
anchors to start-of-line; pytest writes other output (collection
counts, summary banner) on different line shapes that this regex
declines to match.
"""


async def evaluate_patch(
    instance: SWEBenchInstance,
    patch_text: str,
    *,
    arch: str | None = None,
) -> InstanceResult:
    """Run the full SWE-bench eval pipeline for ``instance`` + ``patch_text``.

    Steps:

    1. Pull the per-instance eval image (no-op if cached).
    2. Spin up a fresh container.
    3. Apply ``instance.test_patch`` (surfaces FAIL_TO_PASS tests).
    4. Apply ``patch_text`` (the agent's contribution).
    5. Run every FAIL_TO_PASS + PASS_TO_PASS selector via pytest.
    6. Parse + aggregate.

    Returns an :class:`InstanceResult`. The container is always
    torn down — including on errors — via the async context
    manager exit.
    """

    image = image_name_for_instance(instance, arch=arch)
    try:
        ensure_image_pulled(image)
    except DockerError as exc:
        return InstanceResult(
            instance_id=instance.instance_id,
            image=image,
            patch_applied=False,
            error=f"image pull failed: {exc}",
        )

    async with Container(image=image) as container:
        # Land the test_patch first (always; reveals FAIL_TO_PASS
        # tests even before the agent's patch is in play).
        if instance.test_patch.strip():
            try:
                container.exec(["git", "apply", "-"], input_text=instance.test_patch)
            except DockerError as exc:
                return InstanceResult(
                    instance_id=instance.instance_id,
                    image=image,
                    patch_applied=False,
                    error=f"test_patch apply failed: {exc}",
                )
        # Apply the agent's patch.
        patch_applied = True
        if patch_text.strip():
            try:
                container.exec(["git", "apply", "-"], input_text=patch_text)
            except DockerError as exc:
                # The fail-to-apply path still returns a structured
                # result so operators can see WHICH patch failed +
                # WHICH instance — useful for triage.
                return InstanceResult(
                    instance_id=instance.instance_id,
                    image=image,
                    patch_applied=False,
                    error=f"agent patch apply failed: {exc}",
                )
        selectors = tuple(instance.fail_to_pass) + tuple(instance.pass_to_pass)
        if not selectors:
            # Nothing to test — produce an empty pass result so the
            # eval succeeds vacuously. SWE-bench instances always
            # have selectors in practice; this guard keeps unit
            # tests with empty fixtures sane.
            return InstanceResult(
                instance_id=instance.instance_id,
                image=image,
                patch_applied=patch_applied,
                test_command_exit_code=0,
            )
        run = container.exec(
            ["python", "-m", "pytest", "-v", "--no-header", "-rN", *selectors],
            check=False,
        )
        verdicts = _parse_pytest_output(run.stdout + "\n" + run.stderr)
        f2p = tuple(_score(selector, verdicts) for selector in instance.fail_to_pass)
        p2p = tuple(_score(selector, verdicts) for selector in instance.pass_to_pass)
        return InstanceResult(
            instance_id=instance.instance_id,
            image=image,
            fail_to_pass=f2p,
            pass_to_pass=p2p,
            patch_applied=patch_applied,
            test_command_exit_code=run.returncode,
        )


def _parse_pytest_output(text: str) -> dict[str, TestStatus]:
    """Map ``selector -> status`` for every PASSED / FAILED / ERROR line.

    Later lines win on duplicates (rare; can happen if a selector
    appears in multiple summary sections). Lines we don't recognise
    are skipped.
    """

    verdicts: dict[str, TestStatus] = {}
    for line in text.splitlines():
        match = _PYTEST_LINE_RE.match(line.strip())
        if match is None:
            continue
        verb = match.group("verb")
        selector = match.group("selector")
        status: TestStatus
        if verb == "PASSED":
            status = "passed"
        elif verb == "FAILED":
            status = "failed"
        else:
            status = "error"
        verdicts[selector] = status
    return verdicts


def _score(selector: str, verdicts: dict[str, TestStatus]) -> TestSelectorResult:
    return TestSelectorResult(
        selector=selector,
        status=verdicts.get(selector, "missing"),
    )


__all__ = ["evaluate_patch"]
