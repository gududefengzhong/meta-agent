"""Command implementations for the CLI.

Each ``cmd_*`` coroutine accepts a parsed argparse namespace + a
:class:`TaskClient` and returns an exit code. Output formatting
(stdout = task output, stderr = control / status) is concentrated
here so the network layer in :mod:`meta_agent.cli.client` stays
transport-only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, TextIO

from meta_agent.cli.client import (
    EXIT_OK,
    EXIT_TASK_FAILED,
    CLIError,
    TaskClient,
    is_terminal_state,
)
from meta_agent.infra.observability import (
    LangfuseConfig,
    LangfuseExporterError,
    LangfuseTrajectoryExporter,
)

_sleep = asyncio.sleep


async def cmd_submit(args: argparse.Namespace, client: TaskClient) -> int:
    """Submit a task and print the new task_id on stdout.

    Stays silent on stderr unless ``--verbose`` is passed, so the
    common pattern ``TASK_ID=$(meta-agent submit ...)`` is clean.
    """
    payload = _build_payload(args.prompt, args.payload)
    task = await client.submit_task(
        task_type=args.task_type,
        input_payload=payload,
        idempotency_key=args.idempotency_key,
        session_id=args.session_id,
    )
    task_id = task.get("task_id")
    if not isinstance(task_id, str):
        raise CLIError(2, "server response missing task_id")
    if args.verbose:
        print(f"task submitted: {task_id} ({task.get('state')})", file=sys.stderr)
    print(task_id)
    return EXIT_OK


async def cmd_tail(args: argparse.Namespace, client: TaskClient) -> int:
    """Poll task state for an existing task until terminal."""
    return await _tail_until_terminal(
        client,
        args.task_id,
        events_to=sys.stderr,
        show_events=not args.quiet_events,
    )


async def cmd_run(args: argparse.Namespace, client: TaskClient) -> int:
    """Submit + poll task state in one call."""
    payload = _build_payload(args.prompt, args.payload)
    task = await client.submit_task(
        task_type=args.task_type,
        input_payload=payload,
        idempotency_key=args.idempotency_key,
        session_id=args.session_id,
    )
    task_id = task.get("task_id")
    if not isinstance(task_id, str):
        raise CLIError(2, "server response missing task_id")
    print(f"task: {task_id}", file=sys.stderr)
    return await _tail_until_terminal(
        client,
        task_id,
        events_to=sys.stderr,
        show_events=not args.quiet_events,
    )


async def cmd_trace(args: argparse.Namespace, client: TaskClient) -> int:
    """Fetch and print the merged audit/checkpoint/usage task timeline."""

    page = await client.get_trajectory(args.task_id, limit_per_source=args.limit_per_source)
    items_raw = page.get("items", [])
    items = items_raw if isinstance(items_raw, list) else []
    print(_render_trace_report(args.task_id, items, truncated=bool(page.get("truncated"))))
    return EXIT_OK


async def cmd_export_langfuse(args: argparse.Namespace, client: TaskClient) -> int:
    """Export a persisted task trajectory to Langfuse.

    The command reads Langfuse configuration from process environment
    variables only. It never opens ``.env`` directly; shell tooling
    such as direnv is responsible for loading that file into the
    environment before this command starts.
    """

    try:
        config = LangfuseConfig.require_from_env()
        exporter = LangfuseTrajectoryExporter(config)
        task = await client.get_task(args.task_id)
        trajectory = await client.get_trajectory(
            args.task_id,
            limit_per_source=args.limit_per_source,
        )
        result = await exporter.export_task(
            task_id=args.task_id,
            task=task,
            trajectory=trajectory,
        )
    except LangfuseExporterError as exc:
        raise CLIError(2, str(exc)) from exc
    print(f"langfuse export: trace_id={result.trace_id} observations={result.observation_count}")
    return EXIT_OK


# --------------------------------------------------------------- helpers


def _build_payload(prompt: str | None, payload_json: str | None) -> dict[str, Any]:
    if payload_json is not None:
        try:
            decoded = json.loads(payload_json)
        except ValueError as exc:
            raise CLIError(2, f"--payload is not valid JSON: {exc!s}") from exc
        if not isinstance(decoded, dict):
            raise CLIError(2, "--payload must decode to a JSON object")
        return decoded
    if prompt is None:
        raise CLIError(2, "either a prompt argument or --payload is required")
    return {"user_prompt": prompt}


async def _tail_until_terminal(
    client: TaskClient,
    task_id: str,
    *,
    events_to: TextIO,
    show_events: bool,
) -> int:
    """Poll task state until a terminal state is observed."""

    last_state: str | None = None
    while True:
        task = await client.get_task(task_id)
        state_raw = task.get("state")
        state = state_raw if isinstance(state_raw, str) else None
        if state != last_state and show_events and state is not None:
            events_to.write(f"[state={state}]\n")
            events_to.flush()
        last_state = state
        if is_terminal_state(state):
            if state == "succeeded":
                return EXIT_OK
            return EXIT_TASK_FAILED
        await _sleep(1.0)


def _render_trace_report(task_id: str, items: list[Any], *, truncated: bool) -> str:
    usage_items = [item for item in items if isinstance(item, dict) and item.get("kind") == "usage"]
    audit_items = [item for item in items if isinstance(item, dict) and item.get("kind") == "audit"]
    tool_items = [
        item
        for item in audit_items
        if isinstance(item.get("action"), str) and item["action"].startswith("tool.")
    ]
    total_tokens = sum(_int_or_zero(item.get("total_tokens")) for item in usage_items)
    total_cost = sum(_int_or_zero(item.get("cost_usd_micros")) for item in usage_items)
    total_latency = sum(_int_or_zero(item.get("latency_ms")) for item in usage_items)
    tool_failures = sum(1 for item in tool_items if item.get("action") == "tool.failed")
    llm_failures = sum(
        1 for item in usage_items if str(item.get("status") or "").lower() not in {"ok", ""}
    )

    lines = [
        f"task trace: {task_id}",
        (
            "summary: "
            f"llm_calls={len(usage_items)} "
            f"tool_events={len(tool_items)} "
            f"tool_failures={tool_failures} "
            f"total_tokens={total_tokens} "
            f"cost_usd_micros={total_cost} "
            f"llm_latency_ms={total_latency}"
        ),
    ]
    diagnostics = _trace_failure_diagnostics(
        tool_failures=tool_failures,
        llm_failures=llm_failures,
    )
    if diagnostics:
        lines.append("diagnostics:")
        lines.extend(f"  {line}" for line in diagnostics)
    if truncated:
        lines.append("warning: trajectory truncated by limit_per_source")
    lines.append("")
    lines.append("timeline:")
    for item in items:
        if not isinstance(item, dict):
            continue
        rendered = _render_trace_item(item)
        if rendered:
            lines.append(f"  {rendered}")
    return "\n".join(lines)


def _trace_failure_diagnostics(*, tool_failures: int, llm_failures: int) -> list[str]:
    diagnostics: list[str] = []
    if tool_failures:
        diagnostics.append(
            "category=tool_failed "
            f"summary={tool_failures} tool call(s) returned an error; "
            "inspect tool.failed events around the failing step."
        )
    if llm_failures:
        diagnostics.append(
            "category=llm_failed "
            f"summary={llm_failures} LLM call(s) failed; "
            "inspect usage rows with status != ok."
        )
    return diagnostics


def _render_trace_item(item: dict[str, Any]) -> str:
    occurred = str(item.get("occurred_at", "?"))
    kind = item.get("kind")
    if kind == "usage":
        return (
            f"{occurred} usage "
            f"step={item.get('step_kind') or '-'} "
            f"model={item.get('model') or item.get('requested_model') or '-'} "
            f"tokens={item.get('total_tokens') or 0} "
            f"cost_usd_micros={item.get('cost_usd_micros') or 0} "
            f"latency_ms={item.get('latency_ms') or 0} "
            f"status={item.get('status') or '-'}"
        )
    if kind == "checkpoint":
        return (
            f"{occurred} checkpoint "
            f"node={item.get('node_name') or item.get('current_node') or '-'} "
            f"seq={item.get('sequence') or '-'}"
        )
    if kind != "audit":
        return ""
    action = str(item.get("action") or "-")
    raw_payload = item.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    if action.startswith("tool."):
        return _render_tool_audit(occurred, action, payload)
    return f"{occurred} audit {action}"


def _render_tool_audit(occurred: str, action: str, payload: dict[str, Any]) -> str:
    name = payload.get("tool_name") or "-"
    step = payload.get("agent_step") or "-"
    parts = [f"{occurred} {action} tool={name} step={step}"]
    if "duration_ms" in payload:
        parts.append(f"duration_ms={payload.get('duration_ms')}")
    if "output_bytes" in payload:
        parts.append(f"output_bytes={payload.get('output_bytes')}")
    metadata = payload.get("metadata")
    if isinstance(metadata, dict) and metadata:
        if "exit_code" in metadata:
            parts.append(f"exit_code={metadata['exit_code']}")
        if "permission_outcome" in metadata:
            parts.append(f"permission={metadata['permission_outcome']}")
    args = payload.get("arguments")
    if isinstance(args, dict) and args:
        parts.append(f"args={_render_json_compact(args)}")
    return " ".join(parts)


def _render_json_compact(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(value)


def _int_or_zero(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    return 0
