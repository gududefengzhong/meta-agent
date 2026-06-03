"""Standalone smoke-case runner built on top of the external baseline repo."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from meta_agent.cli.client import EXIT_OK, CLIConfig, CLIError, TaskClient
from meta_agent.cli.commands import _tail_until_terminal
from meta_agent.evals.smoke_catalog import (
    build_payload,
    default_catalog_source,
    default_model,
    default_repo_url,
    default_verify_suite,
    load_cases,
    render_case_summary,
    select_cases,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smoke_case_runner",
        description="Resolve and run bug-fix smoke cases from meta-agent-smoke",
    )
    parser.add_argument(
        "--catalog",
        default=default_catalog_source(),
        help="URL or local override path to meta-agent-smoke catalog/cases.json",
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="Exact smoke case branch, e.g. case/py-safe-join-traversal (repeatable)",
    )
    parser.add_argument(
        "--batch",
        action="append",
        default=[],
        help="Filter by batch (repeatable)",
    )
    parser.add_argument(
        "--category",
        action="append",
        default=[],
        help="Filter by category tag (repeatable)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List matching smoke cases instead of submitting one",
    )
    parser.add_argument(
        "--print-payload",
        action="store_true",
        help="Print the resolved bug-fix payload JSON instead of submitting it",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Submit the selected case and tail it until terminal",
    )
    parser.add_argument(
        "--repo-url",
        default=default_repo_url(),
        help="Repo URL injected into the generated bug-fix payload",
    )
    parser.add_argument(
        "--verify-suite",
        default=default_verify_suite(),
        help="verify_suite injected into the generated bug-fix payload",
    )
    parser.add_argument(
        "--model",
        default=default_model(),
        help="Model injected into the generated bug-fix payload",
    )
    parser.add_argument(
        "--task-type",
        default="bug_fix",
        help="TaskType to submit (default: bug_fix)",
    )
    parser.add_argument(
        "--api-url",
        default=None,
        help="Override $META_AGENT_API_URL (default http://localhost:8000)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Override $META_AGENT_TOKEN",
    )
    parser.add_argument(
        "--idempotency-key",
        default=None,
        help="Optional dedupe key for the submitted smoke task",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Attach the smoke task to an existing session",
    )
    parser.add_argument(
        "--quiet-events",
        action="store_true",
        help="Suppress lifecycle event lines on stderr when --run is used",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Emit extra status lines to stderr",
    )
    return parser


async def run_smoke(args: argparse.Namespace) -> int:
    cases = await load_cases(args.catalog)
    selected = select_cases(
        cases,
        case_names=args.case,
        batches=args.batch,
        categories=args.category,
    )
    if args.list:
        if not selected:
            raise CLIError(2, "no smoke cases matched the requested filters")
        for case in selected:
            print(render_case_summary(case))
        return EXIT_OK
    if not selected:
        raise CLIError(2, "no smoke cases matched the requested filters")
    if len(selected) != 1:
        raise CLIError(
            2,
            "submit/run requires exactly one matching case; "
            "use --list to inspect matches or narrow with --case/--batch/--category",
        )
    payload = build_payload(
        selected[0],
        repo_url=args.repo_url,
        verify_suite=args.verify_suite,
        model=args.model,
    )
    if args.print_payload:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    config = CLIConfig.from_env(api_url=args.api_url, token=args.token)
    async with TaskClient(config) as client:
        task = await client.submit_task(
            task_type=args.task_type,
            input_payload=payload,
            idempotency_key=args.idempotency_key,
            session_id=args.session_id,
        )
        task_id = task.get("task_id")
        if not isinstance(task_id, str):
            raise CLIError(2, "server response missing task_id")
        if not args.run:
            if args.verbose:
                print(f"task submitted: {task_id} ({task.get('state')})", file=sys.stderr)
            print(task_id)
            return EXIT_OK
        print(f"task: {task_id}", file=sys.stderr)
        return await _tail_until_terminal(
            client,
            task_id,
            events_to=sys.stderr,
            show_events=not args.quiet_events,
        )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run_smoke(args))
    except CLIError as exc:
        print(f"smoke_case_runner: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("smoke_case_runner: interrupted", file=sys.stderr)
        return 2
