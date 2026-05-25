"""Score one SWE-bench instance against an agent-produced patch.

The orchestrator pulls the eval image, spins up a container,
applies the patch + the instance's ``test_patch`` (the second is
what surfaces the FAIL_TO_PASS selectors), runs the selectors
through the repo-appropriate test runner picked by
:mod:`eval.swebench.test_specs`, and parses the runner's output
via :mod:`eval.swebench.log_parsers`.

The shape is "patch-in, result-out" so any agent — meta-agent,
human, or another harness — can feed in a candidate patch and get
back a stable comparable result.

Why not just call ``python -m pytest`` directly
================================================
SWE-bench instances span repos that disagree on how to run tests
(Django's ``./tests/runtests.py``, sympy's ``bin/test``, plain
pytest, pytest with ``-rA``, …) and on selector format. The
:class:`TestSpec` layer encodes the right command + parser for
each ``(repo, version)`` so the runner actually collects the right
tests. Phase-1 scope (see ``docs/specs/EVAL_BASELINE.md``) is
pytest-friendly repos only.

Conda env activation
====================
The runner runs inside the eval image's conda env ``testbed``
(``/opt/miniconda3/bin/activate testbed``). Upstream
SWE-bench's eval script does the same activation; we mirror it
so package imports + entry points (``pytest`` etc.) resolve to
the right interpreter. Verified by exec-ing into a real eval
image; ``setup_env.sh`` inside every image creates this env at
exactly this path.

Raw test output
===============
``evaluate_patch`` accepts an optional ``test_output_path`` arg.
When set, the runner's full stdout+stderr lands at that path.
Default behaviour writes nothing — keeps the happy path quiet,
keeps reports deterministic. Use the path for triage when a
parser produces "all selectors missing" or "test_command_exit_code
non-zero but no FAILED line": without raw output, every
diagnosis requires a manual ``docker exec``.
"""

from __future__ import annotations

import logging
import shlex
from pathlib import Path

from eval.swebench.containers import Container, DockerError, ensure_image_pulled
from eval.swebench.images import image_name_for_instance
from eval.swebench.instances import SWEBenchInstance
from eval.swebench.log_parsers import PARSER_BY_NAME
from eval.swebench.results import (
    InstanceResult,
    TestSelectorResult,
    TestStatus,
)
from eval.swebench.test_specs import TestSpecNotFoundError, spec_for

logger = logging.getLogger(__name__)

_CONDA_ACTIVATE = "source /opt/miniconda3/bin/activate testbed"
"""Activate the eval image's testbed conda env before each test run.

The SWE-bench eval images install everything into a conda env
called ``testbed``. ``docker exec`` does not source bash init
files so the env isn't automatically active — without this
prefix, ``pytest`` either falls through to a system Python
(missing the repo's deps) or fails with ``command not found``.
"""


async def evaluate_patch(
    instance: SWEBenchInstance,
    patch_text: str,
    *,
    arch: str | None = None,
    test_output_path: Path | None = None,
    dataset_snapshot: str | None = None,
    harness_version: str | None = None,
) -> InstanceResult:
    """Run the full SWE-bench eval pipeline for ``instance`` + ``patch_text``.

    Steps:

    1. Pull the per-instance eval image (no-op if cached).
    2. Spin up a fresh container.
    3. Apply ``instance.test_patch`` (surfaces FAIL_TO_PASS tests).
    4. Apply ``patch_text`` (the agent's contribution).
    5. Resolve the :class:`TestSpec` for ``(repo, version)``.
    6. Run the spec's test command with selectors appended.
    7. Parse + aggregate.

    Returns an :class:`InstanceResult`. The container is always
    torn down — including on errors — via the async context
    manager exit. When ``test_output_path`` is set, the raw
    runner stdout+stderr lands there for triage. ``dataset_snapshot``
    and ``harness_version``, when set, get stamped onto the
    returned result (EVAL_BASELINE Standards 1 + 2).
    """

    result = await _evaluate_patch_inner(
        instance,
        patch_text,
        arch=arch,
        test_output_path=test_output_path,
    )
    return result.model_copy(
        update={
            "dataset_snapshot": dataset_snapshot,
            "harness_version": harness_version,
        }
    )


async def _evaluate_patch_inner(
    instance: SWEBenchInstance,
    patch_text: str,
    *,
    arch: str | None,
    test_output_path: Path | None,
) -> InstanceResult:
    """Core evaluation flow. Returns an InstanceResult without identity
    stamps — :func:`evaluate_patch` adds those at the end so every
    early-return path picks them up uniformly.
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
        patch_applied = True
        if patch_text.strip():
            try:
                container.exec(["git", "apply", "-"], input_text=patch_text)
            except DockerError as exc:
                return InstanceResult(
                    instance_id=instance.instance_id,
                    image=image,
                    patch_applied=False,
                    error=f"agent patch apply failed: {exc}",
                )
        selectors = tuple(instance.fail_to_pass) + tuple(instance.pass_to_pass)
        if not selectors:
            return InstanceResult(
                instance_id=instance.instance_id,
                image=image,
                patch_applied=patch_applied,
                test_command_exit_code=0,
            )
        try:
            spec = spec_for(instance)
        except TestSpecNotFoundError as exc:
            return InstanceResult(
                instance_id=instance.instance_id,
                image=image,
                patch_applied=patch_applied,
                error=str(exc),
            )
        parser = PARSER_BY_NAME[spec.parser]
        selector_args = " ".join(shlex.quote(s) for s in selectors)
        shell_cmd = f"{_CONDA_ACTIVATE} && {spec.test_cmd} {selector_args}"
        run = container.exec(["bash", "-lc", shell_cmd], check=False)
        raw_output = run.stdout + "\n" + run.stderr
        if test_output_path is not None:
            test_output_path.parent.mkdir(parents=True, exist_ok=True)
            test_output_path.write_text(raw_output, encoding="utf-8")
        verdicts = parser(raw_output)
        f2p = tuple(_score(s, verdicts) for s in instance.fail_to_pass)
        p2p = tuple(_score(s, verdicts) for s in instance.pass_to_pass)
        return InstanceResult(
            instance_id=instance.instance_id,
            image=image,
            fail_to_pass=f2p,
            pass_to_pass=p2p,
            patch_applied=patch_applied,
            test_command_exit_code=run.returncode,
        )


def _score(selector: str, verdicts: dict[str, TestStatus]) -> TestSelectorResult:
    return TestSelectorResult(
        selector=selector,
        status=verdicts.get(selector, "missing"),
    )


__all__ = ["evaluate_patch"]
