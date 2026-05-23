"""Inventory + workspace + eval CLI: ``python -m eval.swebench``.

Commands:

* ``list`` (PR 1) — print every instance in the dataset.
* ``show <instance_id>`` (PR 1) — print one instance as JSON.
* ``prepare <instance_id> --out <dir>`` (PR 2) — clone the repo +
  checkout ``base_commit``. Optional ``--apply-test-patch`` lands
  the instance's test_patch.
* ``diff <workspace> --base-commit <sha>`` (PR 2) — print the
  workspace diff vs ``base_commit``.
* ``evaluate <instance_id> --patch <file|->`` (PR 3) — pull the
  eval image, apply the candidate patch in a container, run the
  FAIL_TO_PASS + PASS_TO_PASS selectors, print the
  :class:`InstanceResult` as JSON. Exit code 0 = resolved, 5 =
  not resolved, other codes for usage / not-found / docker
  errors.
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
from eval.swebench.patches import apply_test_patch, extract_patch
from eval.swebench.workspace import WorkspaceError, prepare_workspace

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_WORKSPACE = 4
EXIT_NOT_RESOLVED = 5
EXIT_DOCKER = 6


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

    p_prepare = sub.add_parser(
        "prepare",
        help="Clone the instance repo + checkout base_commit into a workspace dir",
    )
    p_prepare.add_argument("instance_id")
    p_prepare.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Destination directory for the workspace (must not exist unless --overwrite)",
    )
    p_prepare.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace ``--out`` if it already exists",
    )
    p_prepare.add_argument(
        "--remote-url",
        default=None,
        help=(
            "Override the clone URL. Defaults to https://github.com/{repo}.git; "
            "use a local mirror or file:// path for hermetic runs."
        ),
    )
    p_prepare.add_argument(
        "--apply-test-patch",
        action="store_true",
        help="After checkout, apply the instance's test_patch (surfaces FAIL_TO_PASS tests)",
    )

    p_diff = sub.add_parser(
        "diff",
        help="Print the workspace diff vs an instance's base_commit (the agent's patch)",
    )
    p_diff.add_argument("workspace", type=Path)
    diff_target = p_diff.add_mutually_exclusive_group(required=True)
    diff_target.add_argument(
        "--instance-id",
        help="Resolve base_commit from this instance in the dataset",
    )
    diff_target.add_argument(
        "--base-commit",
        help="Diff against this explicit commit SHA",
    )

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
        if args.command == "prepare":
            return _cmd_prepare(args)
        if args.command == "diff":
            return _cmd_diff(args)
        if args.command == "evaluate":
            return _cmd_evaluate(args)
    except SWEBenchDatasetError as exc:
        print(f"eval.swebench: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except WorkspaceError as exc:
        print(f"eval.swebench: {exc}", file=sys.stderr)
        return EXIT_WORKSPACE
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


def _cmd_prepare(args: argparse.Namespace) -> int:
    try:
        inst = load_instance(args.instance_id, args.dataset)
    except SWEBenchDatasetError as exc:
        print(f"eval.swebench: {exc}", file=sys.stderr)
        return EXIT_NOT_FOUND
    workspace_path = prepare_workspace(
        inst,
        args.out,
        remote_url=args.remote_url,
        overwrite=args.overwrite,
    )
    if args.apply_test_patch and inst.test_patch:
        apply_test_patch(workspace_path, inst.test_patch)
    # Concise stdout for piping into the next command; richer
    # status on stderr so a piped consumer is unsurprised.
    print(workspace_path)
    print(
        f"prepared {inst.instance_id} at {workspace_path} "
        f"(base_commit={inst.base_commit[:12]}"
        f"{', test_patch applied' if args.apply_test_patch and inst.test_patch else ''})",
        file=sys.stderr,
    )
    return EXIT_OK


def _cmd_diff(args: argparse.Namespace) -> int:
    if args.base_commit:
        base = args.base_commit
    else:
        try:
            inst = load_instance(args.instance_id, args.dataset)
        except SWEBenchDatasetError as exc:
            print(f"eval.swebench: {exc}", file=sys.stderr)
            return EXIT_NOT_FOUND
        base = inst.base_commit
    diff = extract_patch(args.workspace, base)
    sys.stdout.write(diff)
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
