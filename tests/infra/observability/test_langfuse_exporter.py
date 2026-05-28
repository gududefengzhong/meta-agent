from __future__ import annotations

from typing import Any

import pytest

from meta_agent.infra.observability.langfuse_exporter import (
    LangfuseConfig,
    LangfuseExporterError,
    LangfuseTrajectoryExporter,
)


class _FakeObservation:
    def __init__(self, call: dict[str, Any]) -> None:
        self._call = call

    def __enter__(self) -> _FakeObservation:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def update(self, **kwargs: Any) -> None:
        self._call.setdefault("updates", []).append(kwargs)


class _FakeLangfuse:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.flushed = False

    def create_trace_id(self, *, seed: str | None = None) -> str:
        assert seed == "trace-1"
        return "0" * 32

    def start_as_current_observation(self, **kwargs: Any) -> _FakeObservation:
        self.calls.append(kwargs)
        return _FakeObservation(self.calls[-1])

    def flush(self) -> None:
        self.flushed = True


def test_langfuse_config_requires_key_pair() -> None:
    assert LangfuseConfig.from_env({}) is None
    with pytest.raises(LangfuseExporterError):
        LangfuseConfig.from_env({"LANGFUSE_PUBLIC_KEY": "pk-only"})


def test_langfuse_config_reads_standard_env_names_without_dotenv() -> None:
    config = LangfuseConfig.require_from_env(
        {
            "LANGFUSE_HOST": "http://localhost:3000/",
            "LANGFUSE_PUBLIC_KEY": "pk-test",
            "LANGFUSE_SECRET_KEY": "sk-test",
        }
    )
    assert config.host == "http://localhost:3000"
    assert config.public_key == "pk-test"


def test_export_task_maps_trajectory_to_langfuse_observations() -> None:
    fake = _FakeLangfuse()
    exporter = LangfuseTrajectoryExporter(
        LangfuseConfig(host="http://lf", public_key="pk", secret_key="sk"),
        client_factory=lambda _config: fake,
    )

    result = exporter.export_task_sync(
        task_id="task-1",
        task={
            "task_id": "task-1",
            "tenant_id": "tenant-1",
            "trace_id": "trace-1",
            "task_type": "system_bug_fix",
            "state": "succeeded",
            "result_status": "succeeded",
            "failure_category": None,
            "node_sequence": 4,
        },
        trajectory={
            "truncated": False,
            "items": [
                {
                    "kind": "usage",
                    "occurred_at": "2026-05-27T00:00:00Z",
                    "record_id": "usage-1",
                    "provider": "openrouter",
                    "model": "deepseek/deepseek-v4-pro",
                    "requested_model": None,
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "cost_usd_micros": 123,
                    "latency_ms": 456,
                    "status": "ok",
                    "prompt_id": "bug_fix_v2.system",
                    "prompt_version": 1,
                    "prompt_excerpt": "SYSTEM: fix bug\n\nUSER: hi",
                    "step_kind": "plan",
                },
                {
                    "kind": "audit",
                    "occurred_at": "2026-05-27T00:00:01Z",
                    "event_id": "event-1",
                    "action": "tool.completed",
                    "payload": {
                        "tool_name": "shell_run",
                        "agent_step": 1,
                        "output": "raw output should not be exported",
                        "arguments": {"command": "pytest tests/unit"},
                        "metadata": {"exit_code": 0},
                    },
                },
                {
                    "kind": "checkpoint",
                    "occurred_at": "2026-05-27T00:00:02Z",
                    "checkpoint_id": "chk-1",
                    "sequence": 2,
                    "node_name": "verify",
                    "finished": False,
                },
            ],
        },
    )

    assert result.trace_id == "0" * 32
    assert result.observation_count == 4
    assert fake.flushed is True
    assert [call["as_type"] for call in fake.calls] == ["span", "generation", "tool", "span"]
    root_metadata = fake.calls[0]["metadata"]
    assert root_metadata["result_status"] == "succeeded"
    assert root_metadata["node_sequence"] == 4
    generation = fake.calls[1]
    assert generation["name"] == "llm:plan"
    assert generation["input"] == "SYSTEM: fix bug\n\nUSER: hi"
    assert generation["usage_details"] == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
    }
    assert generation["cost_details"] == {"total": 0.000123}
    tool_metadata = fake.calls[2]["metadata"]
    assert tool_metadata["tool_name"] == "shell_run"
    assert tool_metadata["metadata.exit_code"] == 0
    assert tool_metadata["arg.command"] == "pytest tests/unit"
    assert "output" not in tool_metadata


def test_export_task_keeps_error_excerpt_but_not_raw_tool_output() -> None:
    fake = _FakeLangfuse()
    exporter = LangfuseTrajectoryExporter(
        LangfuseConfig(host="http://lf", public_key="pk", secret_key="sk"),
        client_factory=lambda _config: fake,
    )

    exporter.export_task_sync(
        task_id="task-1",
        task={
            "task_id": "task-1",
            "tenant_id": "tenant-1",
            "trace_id": "trace-1",
            "task_type": "system_bug_fix",
            "state": "failed",
            "result_status": "failed",
            "failure_category": "tool_failed",
            "node_sequence": 2,
        },
        trajectory={
            "truncated": False,
            "items": [
                {
                    "kind": "audit",
                    "occurred_at": "2026-05-27T00:00:01Z",
                    "event_id": "event-1",
                    "action": "tool.failed",
                    "payload": {
                        "tool_name": "edit_patch_apply",
                        "agent_step": 2,
                        "error_excerpt": "[REDACTED:authorization_header] patch failed",
                        "output": "raw output should not be exported",
                        "metadata": {"exit_code": 1},
                    },
                },
            ],
        },
    )

    tool_metadata = fake.calls[1]["metadata"]
    assert tool_metadata["error_excerpt"] == "[REDACTED:authorization_header] patch failed"
    assert tool_metadata["metadata.exit_code"] == 1
    assert "output" not in tool_metadata
