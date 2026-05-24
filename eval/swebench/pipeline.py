"""End-to-end SWE-bench pipeline: prepare → agent → score → result.

One function that closes the Track B loop:

.. code-block::

    prepare_workspace       (PR #43)
        ↓
    apply_test_patch        (PR #43)  ← surfaces FAIL_TO_PASS
        ↓
    run_agent_on_instance   (this PR)  ← drives shell_agent
        ↓
    extract_patch           (PR #43)  ← captures agent's diff
        ↓
    evaluate_patch          (PR #44)  ← scores inside eval image
        ↓
    InstanceResult

Workspace lifecycle
===================
``run_full_pipeline`` creates the workspace under ``work_root``
(default: a fresh tempdir per call) and does NOT clean it up —
operators want to inspect failed runs. Cleanup is the caller's
responsibility. Passing the same ``work_root`` across calls in a
batch run is safe because the per-instance subdirectory is
namespaced by ``instance_id``.

Why test_patch is applied BEFORE the agent runs
================================================
SWE-bench's FAIL_TO_PASS tests are introduced by ``test_patch``;
they don't exist in ``base_commit``. The agent needs to see them
(so it knows what it's solving for) but must NOT be tempted to
modify them (the grader applies test_patch again at eval time
inside the eval image — the grader-side application is the
authoritative one). We apply test_patch to the workspace before
the agent runs so the failing test is visible; if the agent
edits test files, those edits land in the extracted diff and the
grader re-applies the canonical test_patch on top.
"""

from __future__ import annotations

import logging
from pathlib import Path

from eval.swebench.agent import AgentRunResult, run_agent_on_instance
from eval.swebench.evaluate import evaluate_patch
from eval.swebench.instances import SWEBenchInstance
from eval.swebench.patches import apply_test_patch
from eval.swebench.results import InstanceResult
from eval.swebench.workspace import prepare_workspace
from meta_agent.core.ports.llm import LLMClient

logger = logging.getLogger(__name__)


async def run_full_pipeline(
    instance: SWEBenchInstance,
    *,
    llm: LLMClient,
    work_root: Path | str,
    remote_url: str | None = None,
    arch: str | None = None,
    max_steps: int = 20,
) -> tuple[InstanceResult, AgentRunResult]:
    """Drive prepare → agent → score for one SWE-bench instance.

    Returns the eval :class:`InstanceResult` (the scoring verdict)
    plus the :class:`AgentRunResult` (so operators can mine the
    assistant message + step count without re-running). The
    workspace is left on disk under ``work_root`` for inspection.

    Args:
        instance: The SWE-bench row to evaluate.
        llm: An LLMClient — typically from
            :func:`eval.swebench.llm_factory.build_default_llm`.
            Tests pass a :class:`FakeLLMClient`.
        work_root: Parent directory for the workspace. The actual
            checkout lands at ``work_root / instance.instance_id``.
        remote_url: Override for the clone URL (see
            :func:`prepare_workspace`).
        arch: Docker arch override for the eval image (see
            :func:`evaluate_patch`).
        max_steps: shell_agent max plan iterations (see
            :func:`run_agent_on_instance`).
    """

    work_root_path = Path(work_root).resolve()
    work_root_path.mkdir(parents=True, exist_ok=True)
    workspace_path = work_root_path / instance.instance_id

    prepare_workspace(
        instance,
        workspace_path,
        remote_url=remote_url,
        overwrite=True,
    )
    if instance.test_patch.strip():
        apply_test_patch(workspace_path, instance.test_patch)

    agent_result = await run_agent_on_instance(instance, workspace_path, llm, max_steps=max_steps)
    eval_result = await evaluate_patch(instance, agent_result.patch, arch=arch)
    return eval_result, agent_result


__all__ = ["run_full_pipeline"]
