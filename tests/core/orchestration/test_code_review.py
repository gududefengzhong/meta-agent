"""Unit tests for the built-in ``builtin.code_review`` graph.

The graph is pure-LLM and side-effect free: every test drives a
``FakeLLMClient`` whose handler returns a pre-shaped JSON string, then
asserts how the graph projects (or rejects) that string. No subprocess,
no filesystem, no workspace.
"""

from __future__ import annotations

import json

import pytest

from meta_agent.core.orchestration import END, GraphError, TaskRunState
from meta_agent.core.orchestration.graphs.code_review import (
    _MAX_DIFF_BYTES,
    CODE_REVIEW_GRAPH_ID,
    build_code_review_graph,
)
from tests.core.orchestration._fakes import FakeLLMClient, fake_deps, make_response

_SAMPLE_DIFF = (
    "diff --git a/greet.py b/greet.py\n"
    "--- a/greet.py\n+++ b/greet.py\n"
    "@@ -1,2 +1,3 @@\n"
    " def greet(name):\n"
    '-    return "hi " + name\n'
    '+    return "hi, " + name + "!"\n'
)


def _state(*, diff: str = _SAMPLE_DIFF, extra: dict[str, object] | None = None) -> TaskRunState:
    data: dict[str, object] = {"diff_text": diff}
    if extra:
        data.update(extra)
    return TaskRunState(
        task_id="task-1",
        tenant_id="tenant-1",
        trace_id="trace-1",
        graph_id=CODE_REVIEW_GRAPH_ID,
        data=data,
    )


def _review_payload(
    *,
    verdict: str = "comment",
    summary: str = "Looks fine.",
    findings: list[dict[str, object]] | None = None,
    confidence: float = 0.8,
) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "summary": summary,
            "findings": findings if findings is not None else [],
            "confidence": confidence,
        }
    )


async def test_happy_path_projects_structured_review() -> None:
    finding: dict[str, object] = {
        "category": "test_gap",
        "severity": "minor",
        "file": "greet.py",
        "line_range": "2-3",
        "message": "no test exercises the new punctuation behaviour",
        "suggested_action": "add a unit test for greet('alice')",
    }
    llm = FakeLLMClient(
        handler=lambda _req: make_response(
            content=_review_payload(
                verdict="request_changes",
                summary="Add a test for the new punctuation.",
                findings=[finding],
                confidence=0.7,
            )
        )
    )
    graph = build_code_review_graph(fake_deps(llm))
    state = await graph.run(_state())
    assert state.finished and state.current_node == END
    out = state.data["output"]
    assert isinstance(out, dict)
    assert out["verdict"] == "request_changes"
    assert out["confidence"] == 0.7
    assert out["summary"] == "Add a test for the new punctuation."
    assert out["findings"] == [finding]
    assert out["model_used"] == "fake/echo"


@pytest.mark.parametrize("verdict", ["approve", "request_changes", "comment"])
async def test_all_three_verdicts_succeed_under_scheme_x(verdict: str) -> None:
    llm = FakeLLMClient(
        handler=lambda _req: make_response(content=_review_payload(verdict=verdict))
    )
    graph = build_code_review_graph(fake_deps(llm))
    state = await graph.run(_state())
    assert state.finished
    assert state.data["output"]["verdict"] == verdict  # type: ignore[index]


async def test_malformed_json_raises_graph_error() -> None:
    llm = FakeLLMClient(handler=lambda _req: make_response(content="not json at all"))
    graph = build_code_review_graph(fake_deps(llm))
    with pytest.raises(GraphError, match="not valid JSON"):
        await graph.run(_state())


async def test_fenced_json_is_accepted() -> None:
    fenced = "```json\n" + _review_payload(verdict="approve") + "\n```"
    llm = FakeLLMClient(handler=lambda _req: make_response(content=fenced))
    graph = build_code_review_graph(fake_deps(llm))
    state = await graph.run(_state())
    assert state.data["output"]["verdict"] == "approve"  # type: ignore[index]


async def test_unknown_verdict_fails_schema() -> None:
    bad = json.dumps({"verdict": "merge_now", "summary": "x", "findings": [], "confidence": 0.5})
    llm = FakeLLMClient(handler=lambda _req: make_response(content=bad))
    graph = build_code_review_graph(fake_deps(llm))
    with pytest.raises(GraphError, match="failed schema"):
        await graph.run(_state())


async def test_confidence_out_of_range_fails_schema() -> None:
    bad = _review_payload(confidence=1.5)
    llm = FakeLLMClient(handler=lambda _req: make_response(content=bad))
    graph = build_code_review_graph(fake_deps(llm))
    with pytest.raises(GraphError, match="failed schema"):
        await graph.run(_state())


async def test_too_many_findings_fails_schema() -> None:
    finding: dict[str, object] = {
        "category": "style",
        "severity": "info",
        "file": None,
        "line_range": None,
        "message": "nit",
        "suggested_action": None,
    }
    bad = _review_payload(findings=[finding] * 51)
    llm = FakeLLMClient(handler=lambda _req: make_response(content=bad))
    graph = build_code_review_graph(fake_deps(llm))
    with pytest.raises(GraphError, match="failed schema"):
        await graph.run(_state())


async def test_diff_too_large_rejected_before_llm() -> None:
    llm = FakeLLMClient(handler=lambda _req: pytest.fail("LLM must not be called"))
    graph = build_code_review_graph(fake_deps(llm))
    huge = "a" * (_MAX_DIFF_BYTES + 1)
    with pytest.raises(GraphError, match="max_diff_bytes"):
        await graph.run(_state(diff=huge))
    assert llm.calls == []


async def test_missing_diff_raises() -> None:
    llm = FakeLLMClient(handler=lambda _req: pytest.fail("LLM must not be called"))
    graph = build_code_review_graph(fake_deps(llm))
    bad = TaskRunState(
        task_id="task-1",
        tenant_id="tenant-1",
        trace_id="trace-1",
        graph_id=CODE_REVIEW_GRAPH_ID,
        data={},
    )
    with pytest.raises(GraphError, match="diff_text"):
        await graph.run(bad)


async def test_context_and_pr_title_reach_llm_prompt() -> None:
    captured: list[str] = []

    def handler(req: object) -> object:
        # The handler signature is (LLMRequest) -> LLMResponse; we keep
        # it loose here to avoid importing LLMRequest just for typing.
        from meta_agent.core.ports.llm import LLMRequest

        assert isinstance(req, LLMRequest)
        captured.append(req.messages[1].content)
        return make_response(content=_review_payload(verdict="approve"))

    llm = FakeLLMClient(handler=handler)  # type: ignore[arg-type]
    graph = build_code_review_graph(fake_deps(llm))
    await graph.run(
        _state(extra={"pr_title": "Refactor greet", "context": "Customer requested a comma."})
    )
    assert "Refactor greet" in captured[0]
    assert "Customer requested a comma." in captured[0]


async def test_model_and_temperature_passthrough() -> None:
    seen_models: list[str | None] = []
    seen_temps: list[float | None] = []

    def handler(req: object) -> object:
        from meta_agent.core.ports.llm import LLMRequest

        assert isinstance(req, LLMRequest)
        seen_models.append(req.model)
        seen_temps.append(req.temperature)
        return make_response(content=_review_payload(verdict="approve"))

    llm = FakeLLMClient(handler=handler)  # type: ignore[arg-type]
    graph = build_code_review_graph(fake_deps(llm))
    await graph.run(_state(extra={"model": "anthropic/claude-3-5-sonnet", "temperature": 0.2}))
    assert seen_models == ["anthropic/claude-3-5-sonnet"]
    assert seen_temps == [0.2]
