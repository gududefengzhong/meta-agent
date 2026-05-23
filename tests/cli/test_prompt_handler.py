"""CLI tests for the interactive permission prompt loop.

Drives ``run`` / ``tail`` with mocked HTTP that emits an SSE
``permission.prompt`` mid-stream, an injected :data:`PromptDecider`
that bypasses the terminal-input path, and asserts the resulting
``POST .../decide`` is fired with the expected body.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from typing import Any

import httpx
import pytest

from meta_agent.cli.__main__ import main
from meta_agent.cli.client import (
    EXIT_OK,
    EXIT_TASK_FAILED,
    CLIConfig,
    CLIError,
    TaskClient,
)
from meta_agent.cli.commands import _build_payload, _prompt_user_for_decision

_TASK_BODY = {
    "task_id": "t-1",
    "tenant_id": "ten-1",
    "state": "pending",
    "task_type": "system_shell_agent",
    "trace_id": "tr-1",
    "session_id": None,
    "permission_mode": "approve_each_tool",
    "budget_policy": "none",
    "created_at": "2026-06-23T00:00:00+00:00",
    "updated_at": "2026-06-23T00:00:00+00:00",
}


def _sse(events: Iterable[str]) -> bytes:
    return ("".join(f"data: {e}\n\n" for e in events)).encode("utf-8")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_AGENT_API_URL", "http://test")
    monkeypatch.setenv("META_AGENT_TOKEN", "tok-test")


def _patch_task_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Replace TaskClient + capture every HTTP request through the mock transport."""

    captured: list[httpx.Request] = []

    def capturing(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return handler(req)

    def factory(config: CLIConfig) -> TaskClient:
        transport = httpx.MockTransport(capturing)
        return TaskClient(config, transport=transport)

    from meta_agent.cli import __main__ as cli_main

    monkeypatch.setattr(cli_main, "TaskClient", factory)
    return captured


def _patch_decider(monkeypatch: pytest.MonkeyPatch, *, allow: bool, reason: str | None) -> None:
    """Bypass the terminal-input decider with a scripted one."""

    async def scripted(_prompt: dict[str, Any]) -> tuple[bool, str | None]:
        return allow, reason

    from meta_agent.cli import commands as cmd_module

    monkeypatch.setattr(cmd_module, "_prompt_user_for_decision", scripted)


# ----------------------------------------------------------------- tests


def test_run_with_prompt_allows_action_and_posts_decision(
    env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A prompt mid-stream triggers the decider; allow=True is POSTed; task succeeds."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/tasks" and req.method == "POST":
            return httpx.Response(201, json=_TASK_BODY)
        if req.url.path == "/v1/tasks/t-1/llm-stream":
            return httpx.Response(
                200,
                content=_sse(['{"content_delta":"thinking..."}', "[DONE]"]),
                headers={"content-type": "text/event-stream"},
            )
        if req.url.path == "/v1/tasks/t-1/permissions/stream":
            return httpx.Response(
                200,
                content=_sse(
                    [
                        json.dumps(
                            {
                                "prompt_id": "prm-1",
                                "tenant_id": "ten-1",
                                "task_id": "t-1",
                                "tool_name": "shell",
                                "summary": "run shell",
                                "payload": {"cmd": "ls"},
                                "created_at": "2026-06-23T00:00:00+00:00",
                            }
                        )
                    ]
                ),
                headers={"content-type": "text/event-stream"},
            )
        if req.url.path == "/v1/tasks/t-1/permissions/prm-1/decide":
            return httpx.Response(200, json={"prompt_id": "prm-1", "allow": True})
        if req.url.path == "/v1/tasks/t-1/events":
            return httpx.Response(
                200,
                content=_sse(['{"event_id":"e-1","action":"task.terminal","state":"succeeded"}']),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404, text="unexpected " + req.url.path)

    captured = _patch_task_client(monkeypatch, handler)
    _patch_decider(monkeypatch, allow=True, reason=None)

    code = main(["run", "do the thing"])
    assert code == EXIT_OK

    decide_requests = [
        r for r in captured if r.url.path == "/v1/tasks/t-1/permissions/prm-1/decide"
    ]
    assert len(decide_requests) == 1
    body = json.loads(decide_requests[0].content)
    assert body == {"allow": True}


def test_run_with_prompt_denies_action_and_includes_reason(
    env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A deny decision includes the user's reason in the POST body."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/tasks" and req.method == "POST":
            return httpx.Response(201, json=_TASK_BODY)
        if req.url.path == "/v1/tasks/t-1/llm-stream":
            return httpx.Response(
                200,
                content=_sse(['{"content_delta":"hm"}', "[DONE]"]),
                headers={"content-type": "text/event-stream"},
            )
        if req.url.path == "/v1/tasks/t-1/permissions/stream":
            return httpx.Response(
                200,
                content=_sse(
                    [
                        json.dumps(
                            {
                                "prompt_id": "prm-deny",
                                "tenant_id": "ten-1",
                                "task_id": "t-1",
                                "tool_name": "shell",
                                "summary": "run shell",
                                "payload": {"cmd": "rm -rf /"},
                                "created_at": "2026-06-23T00:00:00+00:00",
                            }
                        )
                    ]
                ),
                headers={"content-type": "text/event-stream"},
            )
        if req.url.path == "/v1/tasks/t-1/permissions/prm-deny/decide":
            return httpx.Response(200, json={"prompt_id": "prm-deny", "allow": False})
        if req.url.path == "/v1/tasks/t-1/events":
            return httpx.Response(
                200,
                content=_sse(['{"event_id":"e-1","action":"task.terminal","state":"succeeded"}']),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404)

    captured = _patch_task_client(monkeypatch, handler)
    _patch_decider(monkeypatch, allow=False, reason="too dangerous")

    code = main(["run", "rm everything"])
    assert code == EXIT_OK

    decide_requests = [r for r in captured if "/decide" in r.url.path]
    assert len(decide_requests) == 1
    body = json.loads(decide_requests[0].content)
    assert body == {"allow": False, "reason": "too dangerous"}


def test_no_interactive_flag_skips_permission_stream_entirely(
    env: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--no-interactive`` means the CLI doesn't subscribe to the permission stream."""

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/tasks" and req.method == "POST":
            return httpx.Response(201, json=_TASK_BODY)
        if req.url.path == "/v1/tasks/t-1/llm-stream":
            return httpx.Response(
                200,
                content=_sse(['{"content_delta":"ok"}', "[DONE]"]),
                headers={"content-type": "text/event-stream"},
            )
        if req.url.path == "/v1/tasks/t-1/events":
            return httpx.Response(
                200,
                content=_sse(['{"event_id":"e-1","action":"task.terminal","state":"succeeded"}']),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404, text="unexpected " + req.url.path)

    captured = _patch_task_client(monkeypatch, handler)
    code = main(["run", "do it", "--no-interactive"])
    assert code == EXIT_OK

    # No request to the permission stream means --no-interactive
    # genuinely skipped that subscription.
    assert all("/permissions/stream" not in r.url.path for r in captured)


async def test_default_decider_renders_prompt_to_stderr_and_reads_stdin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """:func:`_prompt_user_for_decision` displays the prompt + reads y/n via input()."""

    inputs = iter(["y"])
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: next(inputs))

    decision = await _prompt_user_for_decision(
        {
            "prompt_id": "prm-x",
            "tool_name": "shell",
            "summary": "run a thing",
            "payload": {"cmd": "ls"},
        }
    )
    assert decision == (True, None)
    err = capsys.readouterr().err
    assert "[permission] run a thing" in err
    assert "allow? [y/N]" in err


async def test_default_decider_denies_and_captures_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inputs = iter(["n", "looks scary"])
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: next(inputs))

    decision = await _prompt_user_for_decision(
        {
            "prompt_id": "prm-x",
            "tool_name": "shell",
            "summary": "run a thing",
            "payload": {"cmd": "rm -rf"},
        }
    )
    assert decision == (False, "looks scary")


# Reference imports kept lean — the test module only needs these.
_ = (EXIT_TASK_FAILED, CLIError, _build_payload)
