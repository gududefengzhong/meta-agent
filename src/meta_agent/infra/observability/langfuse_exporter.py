"""Best-effort Langfuse export for persisted task trajectories.

The platform keeps PostgreSQL as the source of truth for audit,
metering, and recovery. This module exports a *copy* of a task's
trajectory to Langfuse so operators can inspect the run visually.
Langfuse must not sit on the critical execution path.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol, cast


class LangfuseExporterError(RuntimeError):
    """Raised when a Langfuse export cannot be configured or completed."""


class _LangfuseObservation(Protocol):
    def update(self, **kwargs: Any) -> Any: ...


class _LangfuseClient(Protocol):
    def create_trace_id(self, *, seed: str | None = None) -> str: ...

    def start_as_current_observation(
        self,
        **kwargs: Any,
    ) -> AbstractContextManager[_LangfuseObservation]: ...

    def flush(self) -> Any: ...


LangfuseClientFactory = Callable[["LangfuseConfig"], _LangfuseClient]


_HOST_ENV = "LANGFUSE_HOST"
_PUBLIC_KEY_ENV = "LANGFUSE_PUBLIC_KEY"
_SECRET_KEY_ENV = "LANGFUSE_SECRET_KEY"


@dataclass(frozen=True, slots=True)
class LangfuseConfig:
    """Runtime Langfuse settings read from environment variables."""

    host: str
    public_key: str
    secret_key: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> LangfuseConfig | None:
        source = env if env is not None else os.environ
        public_key = (source.get(_PUBLIC_KEY_ENV) or "").strip()
        secret_key = (source.get(_SECRET_KEY_ENV) or "").strip()
        if not public_key and not secret_key:
            return None
        if not public_key or not secret_key:
            raise LangfuseExporterError(
                f"both {_PUBLIC_KEY_ENV} and {_SECRET_KEY_ENV} must be set for Langfuse export"
            )
        host = (source.get(_HOST_ENV) or "https://cloud.langfuse.com").strip().rstrip("/")
        return cls(host=host, public_key=public_key, secret_key=secret_key)

    @classmethod
    def require_from_env(cls, env: Mapping[str, str] | None = None) -> LangfuseConfig:
        config = cls.from_env(env)
        if config is None:
            raise LangfuseExporterError(
                f"missing Langfuse config: set {_PUBLIC_KEY_ENV} and {_SECRET_KEY_ENV}"
            )
        return config


@dataclass(frozen=True, slots=True)
class LangfuseExportResult:
    """Summary of a completed Langfuse trajectory export."""

    trace_id: str
    observation_count: int


class LangfuseTrajectoryExporter:
    """Export serialized trajectory items into one Langfuse trace."""

    def __init__(
        self,
        config: LangfuseConfig,
        *,
        client_factory: LangfuseClientFactory | None = None,
    ) -> None:
        self._config = config
        self._client_factory = client_factory or _default_client_factory

    async def export_task(
        self,
        *,
        task_id: str,
        task: Mapping[str, Any],
        trajectory: Mapping[str, Any],
    ) -> LangfuseExportResult:
        return await asyncio.to_thread(
            self.export_task_sync,
            task_id=task_id,
            task=task,
            trajectory=trajectory,
        )

    def export_task_sync(
        self,
        *,
        task_id: str,
        task: Mapping[str, Any],
        trajectory: Mapping[str, Any],
    ) -> LangfuseExportResult:
        client = self._client_factory(self._config)
        meta_trace_id = _string_or_none(task.get("trace_id")) or task_id
        langfuse_trace_id = client.create_trace_id(seed=meta_trace_id)
        items = _trajectory_items(trajectory)

        root_metadata = {
            "source": "meta-agent",
            "task_id": task_id,
            "meta_agent_trace_id": meta_trace_id,
            "tenant_id": _string_or_none(task.get("tenant_id")),
            "task_type": _string_or_none(task.get("task_type")),
            "state": _string_or_none(task.get("state")),
            "result_status": _string_or_none(task.get("result_status")),
            "failure_category": _string_or_none(task.get("failure_category")),
            "failure_kind": _string_or_none(task.get("failure_kind")),
            "error_code": _string_or_none(task.get("error_code")),
            "node_sequence": _int_or_none(task.get("node_sequence")),
            "permission_mode": _string_or_none(task.get("permission_mode")),
            "budget_policy": _string_or_none(task.get("budget_policy")),
            "truncated": bool(trajectory.get("truncated")),
        }
        with client.start_as_current_observation(
            trace_context={"trace_id": langfuse_trace_id},
            name=f"meta-agent task {task_id}",
            as_type="span",
            input={"task_id": task_id, "task_type": task.get("task_type")},
            metadata=_drop_none(root_metadata),
        ) as root:
            observation_count = 1
            for item in items:
                if item.get("kind") == "usage":
                    _export_usage(client, item)
                elif item.get("kind") == "audit":
                    _export_audit(client, item)
                elif item.get("kind") == "checkpoint":
                    _export_checkpoint(client, item)
                else:
                    continue
                observation_count += 1
            root.update(
                output={"observations": observation_count, "truncated": trajectory.get("truncated")}
            )

        client.flush()
        return LangfuseExportResult(
            trace_id=langfuse_trace_id,
            observation_count=observation_count,
        )


def _default_client_factory(config: LangfuseConfig) -> _LangfuseClient:
    try:
        from langfuse import Langfuse
    except ImportError as exc:  # pragma: no cover - exercised by packaging, not unit tests
        raise LangfuseExporterError(
            "langfuse package is not installed; install project dependencies before export"
        ) from exc
    return cast(
        _LangfuseClient,
        Langfuse(
            public_key=config.public_key,
            secret_key=config.secret_key,
            host=config.host,
        ),
    )


def _trajectory_items(trajectory: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    raw = trajectory.get("items", ())
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def _export_usage(client: _LangfuseClient, item: Mapping[str, Any]) -> None:
    model = _string_or_none(item.get("model")) or _string_or_none(item.get("requested_model"))
    metadata = _drop_none(
        {
            "kind": "usage",
            "record_id": _string_or_none(item.get("record_id")),
            "provider": _string_or_none(item.get("provider")),
            "requested_model": _string_or_none(item.get("requested_model")),
            "status": _string_or_none(item.get("status")),
            "error_category": _string_or_none(item.get("error_category")),
            "error_message": _string_or_none(item.get("error_message")),
            "prompt_id": _string_or_none(item.get("prompt_id")),
            "prompt_version": item.get("prompt_version"),
            "step_kind": _string_or_none(item.get("step_kind")),
            "occurred_at": _string_or_none(item.get("occurred_at")),
            "latency_ms": _int_or_none(item.get("latency_ms")),
        }
    )
    prompt_excerpt = _string_or_none(item.get("prompt_excerpt"))
    usage_details = _drop_none(
        {
            "input_tokens": _int_or_none(item.get("prompt_tokens")),
            "output_tokens": _int_or_none(item.get("completion_tokens")),
            "total_tokens": _int_or_none(item.get("total_tokens")),
        }
    )
    cost_usd_micros = _int_or_none(item.get("cost_usd_micros"))
    cost_details = {"total": cost_usd_micros / 1_000_000} if cost_usd_micros is not None else None
    name = "llm"
    step_kind = _string_or_none(item.get("step_kind"))
    if step_kind:
        name = f"llm:{step_kind}"
    with client.start_as_current_observation(
        name=name,
        as_type="generation",
        model=model,
        input=prompt_excerpt,
        usage_details=usage_details or None,
        cost_details=cost_details,
        metadata=metadata,
    ):
        pass


def _export_audit(client: _LangfuseClient, item: Mapping[str, Any]) -> None:
    action = _string_or_none(item.get("action")) or "audit"
    payload = item.get("payload")
    safe_payload = _safe_audit_payload(payload if isinstance(payload, Mapping) else {})
    as_type = "tool" if action.startswith("tool.") else "span"
    name = action
    tool_name = safe_payload.get("tool_name")
    if isinstance(tool_name, str) and tool_name:
        name = f"{action}:{tool_name}"
    with client.start_as_current_observation(
        name=name,
        as_type=as_type,
        metadata=_drop_none(
            {
                "kind": "audit",
                "event_id": _string_or_none(item.get("event_id")),
                "action": action,
                "occurred_at": _string_or_none(item.get("occurred_at")),
                **safe_payload,
            }
        ),
    ):
        pass


def _export_checkpoint(client: _LangfuseClient, item: Mapping[str, Any]) -> None:
    node_name = _string_or_none(item.get("node_name")) or _string_or_none(item.get("current_node"))
    name = f"checkpoint:{node_name or 'unknown'}"
    with client.start_as_current_observation(
        name=name,
        as_type="span",
        metadata=_drop_none(
            {
                "kind": "checkpoint",
                "checkpoint_id": _string_or_none(item.get("checkpoint_id")),
                "sequence": _int_or_none(item.get("sequence")),
                "node_name": node_name,
                "awaiting_approval": item.get("awaiting_approval"),
                "finished": item.get("finished"),
                "occurred_at": _string_or_none(item.get("occurred_at")),
            }
        ),
    ):
        pass


def _safe_audit_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Keep operational fields, not arbitrary raw prompt/tool output."""

    allowed = {
        "tool_name",
        "agent_step",
        "duration_ms",
        "output_bytes",
        "error_excerpt",
        "error_category",
        "error_message",
        "gate_id",
        "permission_outcome",
    }
    out: dict[str, Any] = {}
    for key in allowed:
        value = payload.get(key)
        if isinstance(value, str | int | float | bool) or value is None:
            out[key] = value
    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        for key in ("exit_code", "permission_outcome"):
            value = metadata.get(key)
            if isinstance(value, str | int | float | bool) or value is None:
                out[f"metadata.{key}"] = value
    args = payload.get("arguments")
    if isinstance(args, Mapping):
        for key in ("path", "command", "test_command"):
            value = args.get(key)
            if isinstance(value, str):
                out[f"arg.{key}"] = value[:500]
    return out


def _drop_none(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None
