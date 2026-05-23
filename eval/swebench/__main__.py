"""Inventory CLI: ``python -m eval.swebench``.

PR 1 ships two commands:

* ``list`` — print every instance in the dataset (one row per
  instance, tab-separated).
* ``show <instance_id>`` — print the full instance as JSON for
  inspection (problem_statement, gold patch, test selectors).

Execution-flavour commands (``prepare``, ``run``) land in PR 2 once
the agent + image lifecycle pieces ship.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from eval.swebench.dataset import SWEBenchDatasetError, load_instance, load_instances
from eval.swebench.images import image_name_for_instance

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval.swebench",
        description="SWE-bench harness inventory (PR 1: scaffold only)",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=None,
        help="Path to a SWE-bench instances JSON file (default: built-in fixture)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List instances in the dataset")
    p_list.add_argument(
        "--repo",
        action="append",
        default=[],
        help="Filter by repo (org/repo). Repeatable.",
    )
    p_list.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of instances returned",
    )

    p_show = sub.add_parser("show", help="Show one instance as JSON")
    p_show.add_argument("instance_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            return _cmd_list(args)
        if args.command == "show":
            return _cmd_show(args)
    except SWEBenchDatasetError as exc:
        print(f"eval.swebench: {exc}", file=sys.stderr)
        return EXIT_USAGE
    parser.error(f"unknown command: {args.command}")
    return EXIT_USAGE  # pragma: no cover - parser.error exits


def _cmd_list(args: argparse.Namespace) -> int:
    repos = args.repo or None
    instances = load_instances(args.dataset, repos=repos, limit=args.limit)
    if not instances:
        print("(no instances matched)", file=sys.stderr)
        return EXIT_OK
    # Tab-separated for easy piping into awk / cut. Header on stderr
    # so a piped consumer sees only data rows on stdout.
    print("instance_id\trepo\tversion\timage", file=sys.stderr)
    for inst in instances:
        image = image_name_for_instance(inst)
        print(f"{inst.instance_id}\t{inst.repo}\t{inst.version}\t{image}")
    return EXIT_OK


def _cmd_show(args: argparse.Namespace) -> int:
    try:
        inst = load_instance(args.instance_id, args.dataset)
    except SWEBenchDatasetError as exc:
        print(f"eval.swebench: {exc}", file=sys.stderr)
        return EXIT_NOT_FOUND
    payload = inst.model_dump(mode="json")
    payload["image"] = image_name_for_instance(inst)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - executed via python -m
    sys.exit(main())


__all__ = ["EXIT_NOT_FOUND", "EXIT_OK", "EXIT_USAGE", "build_parser", "main"]
