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
import contextlib
import json
import sys
from typing import Any, TextIO

from meta_agent.cli.client import (
    EXIT_OK,
    EXIT_TASK_FAILED,
    CLIError,
    TaskClient,
)


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
    """Stream LLM chunks + lifecycle events for an existing task.

    Returns 0 on SUCCEEDED, EXIT_TASK_FAILED on any other terminal
    state. The two SSE streams run as concurrent asyncio tasks; the
    function returns once either the LLM stream closes (terminal
    event emitted) or the lifecycle stream observes a terminal state.
    """
    return await _tail_until_terminal(
        client,
        args.task_id,
        chunks_to=sys.stdout,
        events_to=sys.stderr,
        show_events=not args.quiet_events,
    )


async def cmd_run(args: argparse.Namespace, client: TaskClient) -> int:
    """Submit + tail in one call — the common interactive workflow."""
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
        chunks_to=sys.stdout,
        events_to=sys.stderr,
        show_events=not args.quiet_events,
    )


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
    chunks_to: TextIO,
    events_to: TextIO,
    show_events: bool,
) -> int:
    """Multiplex LLM chunks + lifecycle events; return exit code from final task state."""

    final_state: dict[str, str | None] = {"value": None}

    async def chunk_loop() -> None:
        async for chunk in client.stream_llm_chunks(task_id):
            content = chunk.get("content_delta") if isinstance(chunk, dict) else None
            if isinstance(content, str) and content:
                chunks_to.write(content)
                chunks_to.flush()

    async def event_loop() -> None:
        async for event in client.stream_events(task_id):
            action = event.get("action") if isinstance(event, dict) else None
            state = event.get("state") if isinstance(event, dict) else None
            if isinstance(state, str):
                final_state["value"] = state
            if show_events and isinstance(action, str):
                events_to.write(f"[{action}]\n")
                events_to.flush()

    chunk_task = asyncio.create_task(chunk_loop(), name="cli-llm-stream")
    event_task = asyncio.create_task(event_loop(), name="cli-events")
    try:
        # The lifecycle stream is the authoritative signal for completion —
        # it emits a synthetic ``task.terminal`` row carrying the final
        # state when the task transitions. The LLM stream typically
        # closes shortly after but is not the source of truth.
        await event_task
    finally:
        chunk_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, CLIError):
            await chunk_task

    chunks_to.write("\n")
    chunks_to.flush()

    if final_state["value"] == "succeeded":
        return EXIT_OK
    return EXIT_TASK_FAILED
