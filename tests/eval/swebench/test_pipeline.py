"""Unit tests for :func:`run_full_pipeline`.

Exercises the prepare → agent → score chain end-to-end against:
- a hermetic ``file://`` git remote (real git via subprocess)
- a FakeLLMClient scripting agent behaviour
- a scripted docker layer (subprocess monkeypatched)

This is the highest-coverage test we have for Track B — it proves
the full vertical works without burning OpenRouter tokens or
pulling real SWE-bench eval images.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from eval.swebench.containers import DockerError
from eval.swebench.instances import SWEBenchInstance
from eval.swebench.pipeline import run_full_pipeline

from eval.swebench import containers
from meta_agent.core.ports.tools import ToolCall
from tests.core.orchestration._fakes import FakeLLMClient, make_response


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_remote(tmp_path: Path) -> tuple[str, str]:
    bare = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    bare.mkdir()
    seed.mkdir()
    _git(bare, "init", "--bare", "--quiet")
    _git(seed, "init", "--quiet", "--initial-branch=main")
    _git(seed, "config", "user.email", "a@b")
    _git(seed, "config", "user.name", "a")
    (seed / "calc.py").write_text("def add(x, y):\n    return x + y\n")
    _git(seed, "add", "calc.py")
    _git(seed, "commit", "-q", "-m", "initial")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=seed, text=True).strip()
    url = f"file://{bare}"
    _git(seed, "remote", "add", "origin", url)
    _git(seed, "push", "-q", "origin", "main")
    return url, sha


def _instance(base_commit: str) -> SWEBenchInstance:
    return SWEBenchInstance(
        instance_id="test__repo-1",
        repo="test/repo",
        base_commit=base_commit,
        problem_statement="Add +1 to calc.add return value.",
        test_patch="",  # Keep the pipeline test focused on the round-trip
        fail_to_pass=("tests/test_calc.py::test_add",),
        pass_to_pass=(),
    )


def _script_docker(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[tuple[int, str, str]],
) -> None:
    """Inject a scripted ``_docker_run`` that emits ``responses`` in order."""

    script = iter(responses)

    def fake(
        cmd: Sequence[str],
        *,
        what: str,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            rc, out, err = next(script)
        except StopIteration as exc:
            raise AssertionError(f"docker script exhausted at {what!r}") from exc
        if check and rc != 0:
            raise DockerError(f"{what} failed: {err or out}")
        return subprocess.CompletedProcess(list(cmd), rc, out, err)

    monkeypatch.setattr(containers, "_docker_run", fake)


async def test_full_pipeline_resolved_when_agent_fixes_the_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end happy path: agent edits the file, eval scores RESOLVED."""

    url, sha = _make_remote(tmp_path)
    edit_call = ToolCall(
        id="c1",
        name="edit_write",
        arguments={
            "path": "calc.py",
            "content": "def add(x, y):\n    return x + y + 1\n",
        },
    )
    llm = FakeLLMClient(
        responses=[
            make_response(
                content="I'll change calc.add to return x + y + 1.",
                tool_calls=(edit_call,),
                finish_reason="tool_call",
            ),
            make_response(content="Done.", finish_reason="stop"),
        ]
    )
    # Docker script: image cached, run, test_patch apply (empty so no
    # exec actually runs — pipeline test_patch is ""), agent patch
    # apply, pytest passes, teardown.
    # ``evaluate_patch`` skips the test_patch exec when patch_text is
    # blank, so the script just has: inspect, run, apply-agent-patch,
    # pytest, rm.
    _script_docker(
        monkeypatch,
        [
            (0, "", ""),  # docker image inspect → cached
            (0, "", ""),  # docker run -d
            (0, "", ""),  # exec: git apply agent patch
            (0, "PASSED tests/test_calc.py::test_add\n", ""),  # pytest
            (0, "", ""),  # docker rm
        ],
    )

    eval_result, agent_result = await run_full_pipeline(
        _instance(sha),
        llm=llm,
        work_root=tmp_path / "eval-runs",
        remote_url=url,
    )

    assert eval_result.resolved is True
    assert agent_result.steps == 2
    assert "+    return x + y + 1" in agent_result.patch
    # Workspace was left on disk for inspection.
    assert (tmp_path / "eval-runs" / "test__repo-1" / "calc.py").exists()


async def test_full_pipeline_not_resolved_when_agent_produces_wrong_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url, sha = _make_remote(tmp_path)
    edit_call = ToolCall(
        id="c1",
        name="edit_write",
        arguments={
            "path": "calc.py",
            "content": "def add(x, y):\n    return 0  # wrong\n",
        },
    )
    llm = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(edit_call,),
                finish_reason="tool_call",
            ),
            make_response(content="Done (wrong, on purpose).", finish_reason="stop"),
        ]
    )
    _script_docker(
        monkeypatch,
        [
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (1, "FAILED tests/test_calc.py::test_add - AssertionError\n", ""),
            (0, "", ""),
        ],
    )
    eval_result, agent_result = await run_full_pipeline(
        _instance(sha),
        llm=llm,
        work_root=tmp_path / "eval-runs",
        remote_url=url,
    )
    assert eval_result.resolved is False
    assert eval_result.fail_to_pass[0].status == "failed"
    assert "+    return 0" in agent_result.patch


async def test_full_pipeline_overwrites_existing_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second run on the same work_root replaces the workspace cleanly."""

    url, sha = _make_remote(tmp_path)
    llm = FakeLLMClient(response=make_response(content="No change needed.", finish_reason="stop"))
    # First pass clones; second pass overwrites + clones again.
    # Each pass: docker (inspect, run, pytest, rm) — no patch apply
    # because the agent produces no changes.
    _script_docker(
        monkeypatch,
        [
            (0, "", ""),
            (0, "", ""),
            (0, "PASSED tests/test_calc.py::test_add\n", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "PASSED tests/test_calc.py::test_add\n", ""),
            (0, "", ""),
        ],
    )
    work_root = tmp_path / "eval-runs"
    await run_full_pipeline(_instance(sha), llm=llm, work_root=work_root, remote_url=url)
    # Mutate the workspace to prove the second prepare overwrites it.
    workspace = work_root / "test__repo-1"
    (workspace / "stale-file").write_text("should be wiped")
    await run_full_pipeline(_instance(sha), llm=llm, work_root=work_root, remote_url=url)
    assert not (workspace / "stale-file").exists()
