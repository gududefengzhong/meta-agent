"""End-to-end-ish tests for the CLI entry point.

Drives :func:`meta_agent.cli.__main__.main` with mocked HTTP +
captured stdout / stderr so we can assert on the full
argparse → dispatch → output → exit-code chain.
"""

from __future__ import annotations

from collections.abc import Iterable

import httpx
import pytest

from meta_agent.cli.__main__ import build_parser, main
from meta_agent.cli.client import (
    EXIT_OK,
    EXIT_TASK_FAILED,
    EXIT_USAGE,
    CLIConfig,
    TaskClient,
)
from meta_agent.cli.commands import _build_payload


def _sse(events: Iterable[str]) -> bytes:
    return ("".join(f"data: {e}\n\n" for e in events)).encode("utf-8")


_TASK_BODY = {
    "task_id": "t-1",
    "tenant_id": "ten-1",
    "state": "pending",
    "task_type": "system_shell_agent",
    "trace_id": "tr-1",
    "session_id": None,
    "permission_mode": "auto",
    "budget_policy": "none",
    "created_at": "2026-06-23T00:00:00+00:00",
    "updated_at": "2026-06-23T00:00:00+00:00",
}


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("META_AGENT_API_URL", "http://test")
    monkeypatch.setenv("META_AGENT_TOKEN", "tok-test")


def test_parser_requires_subcommand() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_parser_recognises_subcommands() -> None:
    parser = build_parser()
    for cmd in ("submit", "tail", "run", "trace"):
        # Both forms accept either a positional prompt or args:
        if cmd in {"tail", "trace"}:
            args = parser.parse_args([cmd, "t-1"])
        else:
            args = parser.parse_args([cmd, "do the thing"])
        assert args.command == cmd


def test_build_payload_prefers_payload_json() -> None:
    out = _build_payload(None, '{"user_prompt": "from json", "extra": 1}')
    assert out == {"user_prompt": "from json", "extra": 1}


def test_build_payload_rejects_non_object_json() -> None:
    from meta_agent.cli.client import CLIError

    with pytest.raises(CLIError) as excinfo:
        _build_payload(None, '["list", "not", "object"]')
    assert excinfo.value.exit_code == EXIT_USAGE


def test_build_payload_requires_prompt_or_payload() -> None:
    from meta_agent.cli.client import CLIError

    with pytest.raises(CLIError) as excinfo:
        _build_payload(None, None)
    assert excinfo.value.exit_code == EXIT_USAGE


def test_main_exits_with_usage_error_when_token_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("META_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("META_AGENT_API_URL", raising=False)
    code = main(["submit", "do the thing"])
    assert code == EXIT_USAGE
    captured = capsys.readouterr()
    assert "missing bearer token" in captured.err


def test_main_submit_prints_task_id_on_stdout(
    env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json=_TASK_BODY)

    _patch_task_client(monkeypatch, handler)
    code = main(["submit", "fix the typo"])
    assert code == EXIT_OK
    out = capsys.readouterr()
    assert out.out.strip() == "t-1"


def test_main_run_streams_chunks_to_stdout_and_exits_0_on_success(
    env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/tasks":
            return httpx.Response(201, json=_TASK_BODY)
        if req.url.path == "/v1/tasks/t-1/llm-stream":
            return httpx.Response(
                200,
                content=_sse(
                    [
                        '{"content_delta":"he"}',
                        '{"content_delta":"llo"}',
                        '{"finish_reason":"stop"}',
                        "[DONE]",
                    ]
                ),
                headers={"content-type": "text/event-stream"},
            )
        if req.url.path == "/v1/tasks/t-1/events":
            return httpx.Response(
                200,
                content=_sse(
                    [
                        '{"event_id":"e-1","action":"task.started"}',
                        '{"event_id":"e-2","action":"task.terminal","state":"succeeded"}',
                    ]
                ),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404, text="unexpected " + req.url.path)

    _patch_task_client(monkeypatch, handler)
    code = main(["run", "fix the typo"])
    assert code == EXIT_OK
    out = capsys.readouterr()
    # LLM chunks landed on stdout
    assert "hello" in out.out
    # Lifecycle events landed on stderr
    assert "task.started" in out.err
    assert "task.terminal" in out.err


def test_main_run_returns_task_failed_when_terminal_state_is_failed(
    env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/tasks":
            return httpx.Response(201, json=_TASK_BODY)
        if req.url.path == "/v1/tasks/t-1/llm-stream":
            return httpx.Response(
                200,
                content=_sse(['{"content_delta":"oops"}', "[DONE]"]),
                headers={"content-type": "text/event-stream"},
            )
        if req.url.path == "/v1/tasks/t-1/events":
            return httpx.Response(
                200,
                content=_sse(['{"event_id":"e-1","action":"task.terminal","state":"failed"}']),
                headers={"content-type": "text/event-stream"},
            )
        return httpx.Response(404)

    _patch_task_client(monkeypatch, handler)
    code = main(["run", "do the broken thing"])
    assert code == EXIT_TASK_FAILED


def test_main_propagates_4xx_from_submit_as_usage_error(
    env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "bad task_type"})

    _patch_task_client(monkeypatch, handler)
    code = main(["submit", "x"])
    assert code == EXIT_USAGE
    out = capsys.readouterr()
    assert "HTTP 400" in out.err
    assert "bad task_type" in out.err


def test_main_trace_prints_trajectory_report(
    env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/tasks/t-1/trajectory":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "kind": "audit",
                            "occurred_at": "2026-06-23T00:00:01Z",
                            "event_id": "e-tool-1",
                            "action": "tool.invoked",
                            "payload": {
                                "tool_name": "fs_read",
                                "agent_step": 1,
                                "arguments": {"path": "src/a.py"},
                            },
                        },
                        {
                            "kind": "usage",
                            "occurred_at": "2026-06-23T00:00:02Z",
                            "record_id": "u-1",
                            "provider": "openrouter",
                            "model": "deepseek/deepseek-v4-pro",
                            "requested_model": None,
                            "prompt_tokens": 10,
                            "completion_tokens": 5,
                            "total_tokens": 15,
                            "cost_usd_micros": 20,
                            "latency_ms": 30,
                            "status": "ok",
                            "error_category": None,
                            "error_message": None,
                            "prompt_id": "bug_fix_v2.system",
                            "prompt_version": 1,
                            "step_kind": "plan",
                        },
                    ],
                    "truncated": False,
                },
            )
        return httpx.Response(404, text="unexpected " + req.url.path)

    _patch_task_client(monkeypatch, handler)
    code = main(["trace", "t-1"])
    assert code == EXIT_OK
    out = capsys.readouterr()
    assert "task trace: t-1" in out.out
    assert "llm_calls=1" in out.out
    assert "total_tokens=15" in out.out
    assert "tool.invoked tool=fs_read" in out.out
    assert "usage step=plan" in out.out


# --------------------------------------------------------------- helpers


def _patch_task_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: object,
) -> None:
    """Replace TaskClient with one that uses an httpx.MockTransport.

    The dispatch in ``__main__._dispatch`` instantiates ``TaskClient``
    via the imported symbol; monkeypatch it on the dispatch module
    so the test handler intercepts every HTTP call. We can't just
    patch ``httpx.AsyncClient`` directly because the transport kwarg
    is needed.
    """

    real_cls = TaskClient

    def factory(config: CLIConfig) -> TaskClient:
        transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
        return real_cls(config, transport=transport)

    from meta_agent.cli import __main__ as cli_main

    monkeypatch.setattr(cli_main, "TaskClient", factory)
