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
from collections.abc import Awaitable, Callable
from typing import Any, TextIO

from meta_agent.cli.client import (
    EXIT_OK,
    EXIT_TASK_FAILED,
    CLIError,
    TaskClient,
)

PromptDecider = Callable[[dict[str, Any]], Awaitable[tuple[bool, str | None]]]
"""Coroutine that turns a permission prompt into ``(allow, reason)``.

Production wiring uses :func:`_prompt_user_for_decision` to ask via
the controlling terminal; tests inject a deterministic decider so
no real stdin is touched.
"""


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

    Interactive permission prompts are handled when
    ``--no-interactive`` is NOT set: a stdin y/n prompt routes the
    user's decision back via ``POST /permissions/.../decide``.
    """
    decider = None if args.no_interactive else _prompt_user_for_decision
    return await _tail_until_terminal(
        client,
        args.task_id,
        chunks_to=sys.stdout,
        events_to=sys.stderr,
        show_events=not args.quiet_events,
        decider=decider,
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
    decider = None if args.no_interactive else _prompt_user_for_decision
    return await _tail_until_terminal(
        client,
        task_id,
        chunks_to=sys.stdout,
        events_to=sys.stderr,
        show_events=not args.quiet_events,
        decider=decider,
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
    decider: PromptDecider | None = None,
) -> int:
    """Multiplex LLM chunks + lifecycle events + permission prompts.

    Returns the exit code from the final task state. When ``decider``
    is supplied (production: terminal y/n; tests: scripted), the
    function also subscribes to the per-task permission stream and
    POSTs each decision back via ``client.decide_permission``.
    """

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

    async def prompt_loop() -> None:
        assert decider is not None  # only spawned when a decider is wired
        async for prompt in client.stream_permission_prompts(task_id):
            prompt_id_raw = prompt.get("prompt_id") if isinstance(prompt, dict) else None
            if not isinstance(prompt_id_raw, str):
                continue
            allow, reason = await decider(prompt)
            with contextlib.suppress(CLIError):
                await client.decide_permission(task_id, prompt_id_raw, allow=allow, reason=reason)

    chunk_task = asyncio.create_task(chunk_loop(), name="cli-llm-stream")
    event_task = asyncio.create_task(event_loop(), name="cli-events")
    prompt_task: asyncio.Task[None] | None = None
    if decider is not None:
        prompt_task = asyncio.create_task(prompt_loop(), name="cli-permissions")
    try:
        # The lifecycle stream is the authoritative signal for completion —
        # it emits a synthetic ``task.terminal`` row carrying the final
        # state when the task transitions. The LLM stream and the
        # permission stream typically close shortly after but are not
        # the source of truth.
        await event_task
    finally:
        chunk_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, CLIError):
            await chunk_task
        if prompt_task is not None:
            prompt_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, CLIError):
                await prompt_task

    chunks_to.write("\n")
    chunks_to.flush()

    if final_state["value"] == "succeeded":
        return EXIT_OK
    return EXIT_TASK_FAILED


_PLAN_PROMPT_TOOL_NAME = "<plan>"


async def _prompt_user_for_decision(
    prompt: dict[str, Any],
) -> tuple[bool, str | None]:
    """Default :data:`PromptDecider` — render the prompt + read y/n from stdin.

    ``input()`` is sync, so we run it via :func:`asyncio.to_thread`
    to keep the event loop responsive for the other streams.
    Output goes to stderr so it doesn't pollute the stdout pipe
    that carries the model's actual answer.

    Plan-mode prompts (``tool_name == "<plan>"``) are rendered as a
    list of proposed tool calls under the assistant's plan text;
    per-tool prompts use the single-tool layout.
    """

    tool = prompt.get("tool_name", "<unknown>")
    sys.stderr.write("\n")
    if tool == _PLAN_PROMPT_TOOL_NAME:
        _render_plan_prompt(prompt)
    else:
        _render_tool_prompt(prompt, tool=tool)
    sys.stderr.write("  allow? [y/N]: ")
    sys.stderr.flush()
    answer = await asyncio.to_thread(input)
    allow = answer.strip().lower() in {"y", "yes"}
    reason: str | None = None
    if not allow:
        sys.stderr.write("  reason (optional, press enter to skip): ")
        sys.stderr.flush()
        reason_input = await asyncio.to_thread(input)
        reason = reason_input.strip() or None
    return allow, reason


def _render_tool_prompt(prompt: dict[str, Any], *, tool: Any) -> None:
    summary = prompt.get("summary") or f"Run tool {tool!r}"
    sys.stderr.write(f"  [permission] {summary}\n")
    payload = prompt.get("payload")
    if payload:
        sys.stderr.write(f"  payload: {_render_json(payload)}\n")


def _render_plan_prompt(prompt: dict[str, Any]) -> None:
    plan_text = prompt.get("summary") or "(no plan text)"
    sys.stderr.write("  [plan] Approve the following plan?\n")
    sys.stderr.write(f"  {plan_text}\n")
    payload = prompt.get("payload")
    tool_calls: Any = payload.get("tool_calls") if isinstance(payload, dict) else None
    if isinstance(tool_calls, list) and tool_calls:
        sys.stderr.write(f"  proposed actions ({len(tool_calls)}):\n")
        for idx, call in enumerate(tool_calls, start=1):
            if not isinstance(call, dict):
                continue
            name = call.get("name", "<unknown>")
            arguments = call.get("arguments", {})
            sys.stderr.write(f"    {idx}. {name}({_render_json(arguments)})\n")
    else:
        sys.stderr.write("  (no tool calls in this plan)\n")


def _render_json(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return repr(value)
