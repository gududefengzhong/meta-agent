"""Inventory + eval CLI: ``python -m eval.swebench``.

Phase-1 scope (see ``docs/specs/EVAL_BASELINE.md``):
single-instance, gold-patch-or-supplied-patch evaluation only.
Batch runner, agent driver, workspace prep, and prediction
pipeline come back in later PRs once the per-instance path is
proven stable end-to-end against real eval images.

Commands:

* ``list`` — print every instance in the dataset.
* ``show <instance_id>`` — print one instance as JSON.
* ``evaluate <instance_id> --patch <file|->`` — pull the
  eval image, apply the patch in a container, run the
  FAIL_TO_PASS + PASS_TO_PASS selectors via the per-repo
  :class:`TestSpec`, print the :class:`InstanceResult` as
  JSON.

Exit codes: 0=ok/resolved, 5=not resolved, 3=instance not found,
6=docker error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from eval.swebench.containers import DockerError
from eval.swebench.dataset import SWEBenchDatasetError, load_instance, load_instances
from eval.swebench.evaluate import evaluate_patch
from eval.swebench.images import image_name_for_instance

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_NOT_RESOLVED = 5
EXIT_DOCKER = 6


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval.swebench",
        description="SWE-bench harness (Phase-1: list / show / evaluate)",
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

    p_eval = sub.add_parser(
        "evaluate",
        help="Score a candidate patch against one instance inside the eval Docker image",
    )
    p_eval.add_argument("instance_id")
    p_eval.add_argument(
        "--patch",
        type=Path,
        required=True,
        help="Patch file to apply (use '-' to read from stdin)",
    )
    p_eval.add_argument(
        "--arch",
        default=None,
        help="Image arch override (x86_64 / arm64). Default: current process arch.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            return _cmd_list(args)
        if args.command == "show":
            return _cmd_show(args)
        if args.command == "evaluate":
            return _cmd_evaluate(args)
    except SWEBenchDatasetError as exc:
        print(f"eval.swebench: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except DockerError as exc:
        print(f"eval.swebench: {exc}", file=sys.stderr)
        return EXIT_DOCKER
    parser.error(f"unknown command: {args.command}")
    return EXIT_USAGE  # pragma: no cover - parser.error exits


def _cmd_list(args: argparse.Namespace) -> int:
    repos = args.repo or None
    instances = load_instances(args.dataset, repos=repos, limit=args.limit)
    if not instances:
        print("(no instances matched)", file=sys.stderr)
        return EXIT_OK
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


def _cmd_evaluate(args: argparse.Namespace) -> int:
    try:
        inst = load_instance(args.instance_id, args.dataset)
    except SWEBenchDatasetError as exc:
        print(f"eval.swebench: {exc}", file=sys.stderr)
        return EXIT_NOT_FOUND
    patch_text = _read_patch(args.patch)
    result = asyncio.run(evaluate_patch(inst, patch_text, arch=args.arch))
    print(result.model_dump_json(indent=2))
    print(result.summary, file=sys.stderr)
    return EXIT_OK if result.resolved else EXIT_NOT_RESOLVED


def _read_patch(path: Path) -> str:
    if str(path) == "-":
        return sys.stdin.read()
    return path.read_text(encoding="utf-8")


if __name__ == "__main__":  # pragma: no cover - executed via python -m
    sys.exit(main())


__all__ = ["EXIT_NOT_FOUND", "EXIT_OK", "EXIT_USAGE", "build_parser", "main"]
