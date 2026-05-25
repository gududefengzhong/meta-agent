"""Unit tests for :mod:`eval.swebench.agent`.

The agent runner exercises ``builtin.shell_agent`` end-to-end —
real graph, real local tools, real workspace. Only the LLM is
faked (via the meta-agent test FakeLLMClient) so we get
deterministic patches without burning OpenRouter tokens.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from eval.swebench.agent import run_agent_on_instance
from eval.swebench.instances import SWEBenchInstance

from meta_agent.core.ports.tools import ToolCall
from tests.core.orchestration._fakes import FakeLLMClient, make_response


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_workspace(tmp_path: Path) -> tuple[Path, str]:
    """Create a workspace with one committed file; return (path, sha)."""

    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "--quiet", "--initial-branch=main")
    _git(ws, "config", "user.email", "a@b")
    _git(ws, "config", "user.name", "a")
    (ws / "calc.py").write_text("def add(x, y):\n    return x + y\n")
    _git(ws, "add", "calc.py")
    _git(ws, "commit", "-q", "-m", "initial")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ws, text=True).strip()
    return ws, sha


def _instance(base_commit: str) -> SWEBenchInstance:
    return SWEBenchInstance(
        instance_id="test__repo-1",
        repo="test/repo",
        base_commit=base_commit,
        problem_statement="Make calc.add return x + y + 1 instead.",
    )


async def test_agent_with_no_tool_calls_returns_empty_patch(tmp_path: Path) -> None:
    """The model decides nothing needs changing; workspace is untouched."""

    ws, sha = _init_workspace(tmp_path)
    client = FakeLLMClient(
        response=make_response(content="No changes necessary.", finish_reason="stop")
    )
    result = await run_agent_on_instance(_instance(sha), ws, client)
    assert result.patch == ""
    assert result.assistant_message == "No changes necessary."
    assert result.steps == 1


async def test_agent_edit_via_tool_call_appears_in_extracted_patch(
    tmp_path: Path,
) -> None:
    """When the model emits an edit_write tool call, the change lands and is captured."""

    ws, sha = _init_workspace(tmp_path)
    edit_call = ToolCall(
        id="c1",
        name="edit_write",
        arguments={
            "path": "calc.py",
            "content": "def add(x, y):\n    return x + y + 1\n",
        },
    )
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(edit_call,),
                finish_reason="tool_call",
            ),
            make_response(content="Done — added +1.", finish_reason="stop"),
        ]
    )
    result = await run_agent_on_instance(_instance(sha), ws, client)
    assert "+    return x + y + 1" in result.patch
    assert result.assistant_message == "Done — added +1."
    assert result.steps == 2


async def test_workspace_not_a_directory_rejected(tmp_path: Path) -> None:
    client = FakeLLMClient()
    with pytest.raises(ValueError, match="not found"):
        await run_agent_on_instance(_instance("abc"), tmp_path / "does-not-exist", client)


async def test_user_prompt_includes_repo_and_commit_context(
    tmp_path: Path,
) -> None:
    """The shell_agent's first LLM call should carry the SWE-bench-shaped prompt."""

    ws, sha = _init_workspace(tmp_path)
    client = FakeLLMClient(response=make_response(content="ok", finish_reason="stop"))
    await run_agent_on_instance(_instance(sha), ws, client)
    first_request = client.calls[0]
    user_msg = first_request.messages[-1]
    assert user_msg.role.value == "user"
    assert "test/repo" in user_msg.content
    assert sha[:12] in user_msg.content
    assert "Make calc.add return x + y + 1" in user_msg.content


async def test_max_steps_cap_passed_through(tmp_path: Path) -> None:
    """When the model keeps requesting tool calls, the cap stops the loop."""

    ws, sha = _init_workspace(tmp_path)
    edit_call = ToolCall(
        id="c1",
        name="edit_write",
        arguments={
            "path": "calc.py",
            "content": "def add(x, y):\n    return x + y + 1\n",
        },
    )

    def loopy(_req: object) -> object:
        return make_response(
            content="",
            tool_calls=(edit_call,),
            finish_reason="tool_call",
        )

    client = FakeLLMClient(handler=loopy)  # type: ignore[arg-type]
    result = await run_agent_on_instance(_instance(sha), ws, client, max_steps=2)
    # The patch is from the workspace mutation (the loop did run).
    # ``steps == max_steps`` indicates the cap was hit.
    assert result.steps == 2
    assert "+    return x + y + 1" in result.patch
