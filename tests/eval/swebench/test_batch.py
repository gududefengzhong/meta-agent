"""Unit tests for :func:`run_batch`.

Doesn't re-test ``run_full_pipeline`` (that's covered in
``test_pipeline.py``). Instead, monkeypatches the pipeline to
return canned outcomes per instance so we can validate the
aggregation contract independently: progress callback fires,
per-instance errors don't kill the batch, pass@1 math is right.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from eval.swebench.agent import AgentRunResult
from eval.swebench.batch import run_batch, score_gold_batch
from eval.swebench.instances import SWEBenchInstance
from eval.swebench.results import InstanceReport, InstanceResult, TestSelectorResult

from eval.swebench import batch as batch_module
from tests.core.orchestration._fakes import FakeLLMClient


def _instance(instance_id: str) -> SWEBenchInstance:
    return SWEBenchInstance(
        instance_id=instance_id,
        repo="test/repo",
        base_commit="abc",
    )


def _resolved_result(instance_id: str) -> InstanceResult:
    return InstanceResult(
        instance_id=instance_id,
        image="img:latest",
        fail_to_pass=(TestSelectorResult(selector="t::a", status="passed"),),
        pass_to_pass=(),
        patch_applied=True,
        test_command_exit_code=0,
    )


def _failed_result(instance_id: str) -> InstanceResult:
    return InstanceResult(
        instance_id=instance_id,
        image="img:latest",
        fail_to_pass=(TestSelectorResult(selector="t::a", status="failed"),),
        pass_to_pass=(),
        patch_applied=True,
        test_command_exit_code=1,
    )


class _ScriptedPipeline:
    """Per-instance scripted outcomes for ``run_full_pipeline``.

    Each entry is either an (InstanceResult, AgentRunResult) tuple
    (success path) or an Exception (the pipeline raised). Looked
    up by ``instance_id``; missing ids fall through to a default
    resolved result so tests stay terse.
    """

    def __init__(
        self,
        outcomes: dict[str, tuple[InstanceResult, AgentRunResult] | Exception],
    ) -> None:
        self._outcomes = outcomes
        self.calls: list[str] = []

    async def __call__(
        self,
        instance: SWEBenchInstance,
        *,
        llm: object,
        work_root: Path | str,
        remote_url: str | None = None,
        arch: str | None = None,
        max_steps: int = 20,
    ) -> tuple[InstanceResult, AgentRunResult]:
        self.calls.append(instance.instance_id)
        outcome = self._outcomes.get(instance.instance_id)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            outcome = (
                _resolved_result(instance.instance_id),
                AgentRunResult(patch="diff", assistant_message="ok", steps=3),
            )
        return outcome


@pytest.fixture
def script(monkeypatch: pytest.MonkeyPatch) -> _ScriptedPipeline:
    scripted = _ScriptedPipeline({})
    monkeypatch.setattr(batch_module, "run_full_pipeline", scripted)
    return scripted


async def test_empty_batch_returns_zero_total(tmp_path: Path) -> None:
    report = await run_batch([], llm=FakeLLMClient(), work_root=tmp_path)
    assert report.total == 0
    assert report.resolved == 0
    assert report.pass_at_1 == 0.0
    assert report.instances == ()


async def test_all_resolved_batch_yields_pass_at_1_of_1(
    tmp_path: Path, script: _ScriptedPipeline
) -> None:
    instances = [_instance(f"x__y-{i}") for i in range(3)]
    report = await run_batch(instances, llm=FakeLLMClient(), work_root=tmp_path)
    assert report.total == 3
    assert report.resolved == 3
    assert report.not_resolved == 0
    assert report.errored == 0
    assert report.pass_at_1 == 1.0


async def test_mixed_outcomes_aggregate_correctly(
    tmp_path: Path, script: _ScriptedPipeline
) -> None:
    script._outcomes = {
        "x__y-0": (_resolved_result("x__y-0"), AgentRunResult("p1", "ok", 2)),
        "x__y-1": (_failed_result("x__y-1"), AgentRunResult("wrong", "tried", 5)),
        "x__y-2": RuntimeError("clone exploded"),
    }
    report = await run_batch(
        [_instance(f"x__y-{i}") for i in range(3)],
        llm=FakeLLMClient(),
        work_root=tmp_path,
    )
    assert report.total == 3
    assert report.resolved == 1
    assert report.not_resolved == 1
    assert report.errored == 1
    assert abs(report.pass_at_1 - 1 / 3) < 1e-9


async def test_per_instance_error_recorded_with_typed_message(
    tmp_path: Path, script: _ScriptedPipeline
) -> None:
    script._outcomes = {
        "boom": RuntimeError("docker daemon refused connection"),
    }
    report = await run_batch(
        [_instance("boom"), _instance("ok")],
        llm=FakeLLMClient(),
        work_root=tmp_path,
    )
    boom_row = next(r for r in report.instances if r.instance_id == "boom")
    assert boom_row.error == "RuntimeError: docker daemon refused connection"
    assert boom_row.result is None
    ok_row = next(r for r in report.instances if r.instance_id == "ok")
    assert ok_row.error is None
    assert ok_row.result is not None
    assert ok_row.result.resolved is True


async def test_progress_callback_fires_per_instance(
    tmp_path: Path, script: _ScriptedPipeline
) -> None:
    rows: list[InstanceReport] = []
    await run_batch(
        [_instance(f"x__y-{i}") for i in range(4)],
        llm=FakeLLMClient(),
        work_root=tmp_path,
        progress=rows.append,
    )
    assert [r.instance_id for r in rows] == ["x__y-0", "x__y-1", "x__y-2", "x__y-3"]


async def test_progress_callback_failure_does_not_kill_batch(
    tmp_path: Path, script: _ScriptedPipeline
) -> None:
    """A buggy progress callback shouldn't abort a long benchmark run."""

    def broken(_row: InstanceReport) -> None:
        raise RuntimeError("buggy renderer")

    report = await run_batch(
        [_instance(f"x__y-{i}") for i in range(2)],
        llm=FakeLLMClient(),
        work_root=tmp_path,
        progress=broken,
    )
    assert report.total == 2
    assert report.resolved == 2  # despite the callback exceptions


async def test_instances_consumed_in_order_passed(
    tmp_path: Path, script: _ScriptedPipeline
) -> None:
    instances = [_instance("a"), _instance("b"), _instance("c")]
    await run_batch(instances, llm=FakeLLMClient(), work_root=tmp_path)
    assert script.calls == ["a", "b", "c"]


async def test_report_round_trips_through_json(tmp_path: Path, script: _ScriptedPipeline) -> None:
    """BatchReport serialises + parses without losing fields."""

    from eval.swebench.results import BatchReport

    script._outcomes = {
        "x": (_resolved_result("x"), AgentRunResult("p", "msg", 1)),
        "y": RuntimeError("oops"),
    }
    report = await run_batch(
        [_instance("x"), _instance("y")], llm=FakeLLMClient(), work_root=tmp_path
    )
    payload = report.model_dump_json()
    parsed = BatchReport.model_validate_json(payload)
    assert parsed.total == 2
    assert parsed.resolved == 1
    assert parsed.errored == 1
    assert parsed.instances[1].error and "RuntimeError" in parsed.instances[1].error


def test_batch_report_summary_renders_pass_at_1_percentage() -> None:
    from eval.swebench.results import BatchReport

    report = BatchReport(
        total=4,
        resolved=3,
        not_resolved=1,
        errored=0,
        duration_seconds=12.3,
        instances=(),
    )
    text = report.summary
    assert "3/4 resolved" in text
    assert "75.0%" in text
    assert "12.3s wall" in text


def test_batch_report_pass_at_1_empty_batch_is_zero() -> None:
    from eval.swebench.results import BatchReport

    empty = BatchReport(
        total=0,
        resolved=0,
        not_resolved=0,
        errored=0,
        duration_seconds=0.0,
        instances=(),
    )
    assert empty.pass_at_1 == 0.0


# ----------------------------------------------------------- score_gold_batch


def _gold_instance(instance_id: str, *, patch: str = "diff --git a/x b/x\n") -> SWEBenchInstance:
    return SWEBenchInstance(
        instance_id=instance_id,
        repo="test/repo",
        base_commit="abc",
        patch=patch,
    )


class _ScriptedEvaluator:
    """Per-instance scripted outcomes for ``evaluate_patch``."""

    def __init__(self, outcomes: dict[str, InstanceResult | Exception]) -> None:
        self._outcomes = outcomes
        self.calls: list[tuple[str, str]] = []

    async def __call__(
        self,
        instance: SWEBenchInstance,
        patch_text: str,
        *,
        arch: str | None = None,
    ) -> InstanceResult:
        self.calls.append((instance.instance_id, patch_text))
        outcome = self._outcomes.get(instance.instance_id)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            return _resolved_result(instance.instance_id)
        return outcome


@pytest.fixture
def gold_eval(monkeypatch: pytest.MonkeyPatch) -> _ScriptedEvaluator:
    scripted = _ScriptedEvaluator({})
    monkeypatch.setattr(batch_module, "evaluate_patch", scripted)
    return scripted


async def test_gold_batch_all_resolved_yields_pass_at_1_of_1(
    gold_eval: _ScriptedEvaluator,
) -> None:
    report = await score_gold_batch([_gold_instance(f"x__y-{i}") for i in range(3)])
    assert report.total == 3
    assert report.resolved == 3
    assert report.pass_at_1 == 1.0
    # Each call must have fed the instance's gold patch through.
    assert [patch for _, patch in gold_eval.calls] == ["diff --git a/x b/x\n"] * 3


async def test_gold_batch_empty_gold_patch_lands_as_error(
    gold_eval: _ScriptedEvaluator,
) -> None:
    instances = [
        _gold_instance("has-patch"),
        _gold_instance("no-patch", patch="   "),
    ]
    report = await score_gold_batch(instances)
    assert report.errored == 1
    assert report.resolved == 1
    no_patch_row = next(r for r in report.instances if r.instance_id == "no-patch")
    assert no_patch_row.error == "dataset row has empty gold patch"
    assert no_patch_row.result is None
    # ``evaluate_patch`` should not have been called for the empty row.
    assert [name for name, _ in gold_eval.calls] == ["has-patch"]


async def test_gold_batch_evaluate_exception_recorded_as_error(
    gold_eval: _ScriptedEvaluator,
) -> None:
    gold_eval._outcomes = {"boom": RuntimeError("docker daemon refused connection")}
    report = await score_gold_batch([_gold_instance("boom"), _gold_instance("ok")])
    boom_row = next(r for r in report.instances if r.instance_id == "boom")
    assert boom_row.error == "RuntimeError: docker daemon refused connection"
    assert boom_row.result is None
    assert report.errored == 1
    assert report.resolved == 1


async def test_gold_batch_failed_gold_patch_counts_as_not_resolved(
    gold_eval: _ScriptedEvaluator,
) -> None:
    gold_eval._outcomes = {"buggy": _failed_result("buggy")}
    report = await score_gold_batch([_gold_instance("buggy")])
    assert report.not_resolved == 1
    assert report.resolved == 0
    assert report.errored == 0
    assert report.pass_at_1 == 0.0


async def test_gold_batch_progress_callback_fires_per_instance(
    gold_eval: _ScriptedEvaluator,
) -> None:
    rows: list[InstanceReport] = []
    await score_gold_batch(
        [_gold_instance(f"x__y-{i}") for i in range(2)],
        progress=rows.append,
    )
    assert [r.instance_id for r in rows] == ["x__y-0", "x__y-1"]


async def test_gold_batch_progress_callback_failure_does_not_kill_batch(
    gold_eval: _ScriptedEvaluator,
) -> None:
    def broken(_row: InstanceReport) -> None:
        raise RuntimeError("buggy renderer")

    report = await score_gold_batch(
        [_gold_instance(f"x__y-{i}") for i in range(2)],
        progress=broken,
    )
    assert report.total == 2
    assert report.resolved == 2
