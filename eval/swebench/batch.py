"""Batch runner: drive the SWE-bench pipeline across a list of instances.

PR #46 closed the per-instance loop. This module makes the
benchmark *actually usable*: you give it a list of instances and
it produces a :class:`BatchReport` with the pass@1 number Track B
promised.

Concurrency
===========
v0 runs instances **serially**. SWE-bench evaluations are heavy
(git clone + agent thinking + container start + pytest), and
running them in parallel would compete for Docker daemon
bandwidth + disk + the LLM provider's rate limit. Adding
``--concurrency`` is a one-liner later when we see real
benchmark-time wallclock pressure.

Per-instance isolation
======================
Every instance gets its own workspace under ``work_root /
instance_id``. A pipeline failure for one instance (clone error,
docker oom, etc.) is captured into the per-instance report row
and the batch continues — one bad instance must not poison a
500-instance run.

Progress observation
====================
The runner accepts an optional ``progress`` callback so the CLI
can render incremental output ("instance 7/500 RESOLVED in 42s").
Default is a no-op so library callers don't pay rendering cost.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from eval.swebench.evaluate import evaluate_patch
from eval.swebench.instances import SWEBenchInstance
from eval.swebench.pipeline import run_full_pipeline
from eval.swebench.results import BatchReport, InstanceReport
from meta_agent.core.ports.llm import LLMClient

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[InstanceReport], None]
"""Invoked once per completed instance with its row.

The runner calls it after each pipeline finishes (success or
error). The callback runs synchronously inline — keep it cheap
or it will dominate wallclock for fast instances.
"""


async def run_batch(
    instances: Iterable[SWEBenchInstance],
    *,
    llm: LLMClient,
    work_root: Path | str,
    remote_url: str | None = None,
    arch: str | None = None,
    max_steps: int = 20,
    progress: ProgressCallback | None = None,
) -> BatchReport:
    """Run :func:`run_full_pipeline` for each instance; aggregate the results.

    Per-instance pipeline errors are caught and recorded in the
    :class:`InstanceReport.error` column — the batch always
    completes once every instance has been attempted. The
    ``error`` field is the stringified exception; for richer
    triage operators rerun a single instance via the per-instance
    ``run-agent`` command.
    """

    work_root_path = Path(work_root).resolve()
    work_root_path.mkdir(parents=True, exist_ok=True)

    rows: list[InstanceReport] = []
    resolved = 0
    not_resolved = 0
    errored = 0
    batch_started = time.monotonic()

    for instance in instances:
        row = await _run_one(
            instance,
            llm=llm,
            work_root=work_root_path,
            remote_url=remote_url,
            arch=arch,
            max_steps=max_steps,
        )
        rows.append(row)
        if row.error is not None:
            errored += 1
        elif row.result is not None and row.result.resolved:
            resolved += 1
        else:
            not_resolved += 1
        if progress is not None:
            try:
                progress(row)
            except Exception:
                # A buggy progress callback shouldn't kill a long
                # batch. Log and continue.
                logger.exception("swebench.batch.progress_callback_failed")

    duration = time.monotonic() - batch_started
    return BatchReport(
        total=len(rows),
        resolved=resolved,
        not_resolved=not_resolved,
        errored=errored,
        duration_seconds=duration,
        instances=tuple(rows),
    )


async def _run_one(
    instance: SWEBenchInstance,
    *,
    llm: LLMClient,
    work_root: Path,
    remote_url: str | None,
    arch: str | None,
    max_steps: int,
) -> InstanceReport:
    """Single-instance wrapper that catches any pipeline failure."""

    started = time.monotonic()
    try:
        eval_result, agent_result = await run_full_pipeline(
            instance,
            llm=llm,
            work_root=work_root,
            remote_url=remote_url,
            arch=arch,
            max_steps=max_steps,
        )
    except Exception as exc:
        return InstanceReport(
            instance_id=instance.instance_id,
            result=None,
            error=f"{type(exc).__name__}: {exc}",
            duration_seconds=time.monotonic() - started,
        )
    return InstanceReport(
        instance_id=instance.instance_id,
        result=eval_result,
        error=None,
        duration_seconds=time.monotonic() - started,
        agent_steps=agent_result.steps,
        patch_size_bytes=len(agent_result.patch.encode("utf-8")),
    )


async def score_gold_batch(
    instances: Iterable[SWEBenchInstance],
    *,
    arch: str | None = None,
    progress: ProgressCallback | None = None,
) -> BatchReport:
    """Score the dataset's gold patches through the eval harness.

    Used as a harness self-check: every gold patch is the human-
    written reference fix, so pass@1 here should be 1.0 modulo
    instances whose gold patch is empty or whose image is missing.
    A regression in the harness (image resolver bug, pytest parser
    drift, container teardown breakage) shows up as gold patches
    that suddenly stop resolving — which is exactly what the
    CI gate is meant to catch.

    The agent / workspace / LLM are never involved; this calls
    :func:`evaluate_patch` directly with ``instance.patch``.
    Instances with an empty ``patch`` field land as ``error`` rows
    rather than silently passing — a missing reference patch is a
    dataset problem, not a harness pass.
    """

    rows: list[InstanceReport] = []
    resolved = 0
    not_resolved = 0
    errored = 0
    batch_started = time.monotonic()

    for instance in instances:
        row = await _score_one_gold(instance, arch=arch)
        rows.append(row)
        if row.error is not None:
            errored += 1
        elif row.result is not None and row.result.resolved:
            resolved += 1
        else:
            not_resolved += 1
        if progress is not None:
            try:
                progress(row)
            except Exception:
                logger.exception("swebench.batch.gold_progress_callback_failed")

    duration = time.monotonic() - batch_started
    return BatchReport(
        total=len(rows),
        resolved=resolved,
        not_resolved=not_resolved,
        errored=errored,
        duration_seconds=duration,
        instances=tuple(rows),
    )


async def _score_one_gold(
    instance: SWEBenchInstance,
    *,
    arch: str | None,
) -> InstanceReport:
    started = time.monotonic()
    if not instance.patch.strip():
        return InstanceReport(
            instance_id=instance.instance_id,
            result=None,
            error="dataset row has empty gold patch",
            duration_seconds=time.monotonic() - started,
        )
    try:
        eval_result = await evaluate_patch(instance, instance.patch, arch=arch)
    except Exception as exc:
        return InstanceReport(
            instance_id=instance.instance_id,
            result=None,
            error=f"{type(exc).__name__}: {exc}",
            duration_seconds=time.monotonic() - started,
        )
    return InstanceReport(
        instance_id=instance.instance_id,
        result=eval_result,
        error=None,
        duration_seconds=time.monotonic() - started,
        agent_steps=0,
        patch_size_bytes=len(instance.patch.encode("utf-8")),
    )


__all__ = ["ProgressCallback", "run_batch", "score_gold_batch"]
