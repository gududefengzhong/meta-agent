"""Argparse dispatch for ``python -m meta_agent.cli``.

Keeps the parser construction separate from the async command
bodies so unit tests can exercise the routing logic without
spinning up the asyncio event loop.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable

from meta_agent.cli.client import (
    EXIT_OK,
    EXIT_USAGE,
    CLIConfig,
    CLIError,
    TaskClient,
)
from meta_agent.cli.commands import cmd_run, cmd_submit, cmd_tail, cmd_trace

CommandFn = Callable[[argparse.Namespace, TaskClient], Awaitable[int]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meta-agent",
        description="meta-agent code agent CLI (v0)",
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help="Override $META_AGENT_API_URL (default http://localhost:8000)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Override $META_AGENT_TOKEN (bearer token for /v1/* calls)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Emit extra status lines to stderr",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    p_submit = sub.add_parser("submit", help="Submit a task; print task_id")
    _add_task_args(p_submit)
    p_submit.set_defaults(func=cmd_submit)

    p_tail = sub.add_parser("tail", help="Stream chunks + events for a task")
    p_tail.add_argument("task_id")
    p_tail.add_argument(
        "--quiet-events",
        action="store_true",
        help="Suppress lifecycle event lines on stderr (chunks still print)",
    )
    p_tail.add_argument(
        "--no-interactive",
        action="store_true",
        help=(
            "Skip the interactive permission prompt handler. Inline "
            "permission prompts emitted by the worker will be ignored "
            "(and the agent will wait until its 120s timeout)."
        ),
    )
    p_tail.set_defaults(func=cmd_tail)

    p_trace = sub.add_parser("trace", help="Print a task trajectory report")
    p_trace.add_argument("task_id")
    p_trace.add_argument(
        "--limit-per-source",
        type=int,
        default=1000,
        help="Maximum rows to fetch from each trajectory source (default 1000)",
    )
    p_trace.set_defaults(func=cmd_trace)

    p_run = sub.add_parser("run", help="Submit a task and stream until terminal")
    _add_task_args(p_run)
    p_run.add_argument(
        "--quiet-events",
        action="store_true",
        help="Suppress lifecycle event lines on stderr",
    )
    p_run.add_argument(
        "--no-interactive",
        action="store_true",
        help=("Skip the interactive permission prompt handler (same semantics as on ``tail``)."),
    )
    p_run.set_defaults(func=cmd_run)

    return parser


def _add_task_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "prompt",
        nargs="?",
        default=None,
        help="Natural-language task prompt (sent as input_payload.user_prompt)",
    )
    parser.add_argument(
        "--task-type",
        default="system_shell_agent",
        help="TaskType to submit (default: system_shell_agent)",
    )
    parser.add_argument(
        "--payload",
        default=None,
        help="JSON-encoded input_payload override (mutually exclusive with prompt)",
    )
    parser.add_argument(
        "--idempotency-key",
        default=None,
        help="Optional dedupe key; second submission with the same key returns the original task",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Attach the task to an existing session for multi-turn conversations",
    )


async def _dispatch(args: argparse.Namespace) -> int:
    config = CLIConfig.from_env(api_url=args.api_url, token=args.token)
    async with TaskClient(config) as client:
        func: CommandFn = args.func
        return await func(args, client)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns an exit code instead of calling ``sys.exit``.

    Unit tests pass an explicit ``argv`` and assert on the returned
    int; the ``__main__`` block at module bottom converts it to
    ``sys.exit`` only when run as a script.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_dispatch(args))
    except CLIError as exc:
        print(f"meta-agent: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("meta-agent: interrupted", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover - executed via python -m
    sys.exit(main())


# Re-export for tests
__all__ = ["EXIT_OK", "build_parser", "main"]
