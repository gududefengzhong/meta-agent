"""Inventory + eval CLI: ``python -m eval.swebench``.

Commands:

* ``list`` — print every instance in the dataset.
* ``show <instance_id>`` — print one instance as JSON.
* ``evaluate <instance_id> --patch <file|->`` — pull the eval
  image, apply the patch in a container, run the FAIL_TO_PASS +
  PASS_TO_PASS selectors via the per-repo :class:`TestSpec`,
  print the :class:`InstanceResult` as JSON.
* ``run-agent <instance_id> --work-root <dir>`` — full pipeline:
  prepare workspace + drive ``builtin.shell_agent`` + score.
  Requires ``OPENROUTER_API_KEY`` (or ``--api-key``) since this
  hits a real LLM.

Exit codes: 0=ok/resolved, 5=not resolved, 3=instance not found,
4=workspace error, 6=docker error, 7=LLM config error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from eval.swebench.agent import PROMPT_VERSION
from eval.swebench.containers import DockerError
from eval.swebench.dataset import (
    SWEBenchDatasetError,
    builtin_dataset_path,
    load_instance,
    load_instances,
)
from eval.swebench.evaluate import evaluate_patch
from eval.swebench.identity import dataset_snapshot, harness_version
from eval.swebench.images import image_name_for_instance
from eval.swebench.llm_factory import EvalLLMConfigError, build_default_llm
from eval.swebench.pipeline import run_full_pipeline
from eval.swebench.workspace import WorkspaceError

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_WORKSPACE = 4
EXIT_NOT_RESOLVED = 5
EXIT_DOCKER = 6
EXIT_LLM_CONFIG = 7


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
    p_eval.add_argument(
        "--log-test-output",
        type=Path,
        default=None,
        help=(
            "Optional path to write the test runner's raw stdout+stderr. "
            "Use this when a result has 'all selectors missing' or pytest exited "
            "non-zero with no FAILED line — without it, every diagnosis means "
            "exec-ing into the container manually."
        ),
    )

    p_run = sub.add_parser(
        "run-agent",
        help="Full pipeline: prepare workspace + drive shell_agent + score",
    )
    p_run.add_argument("instance_id")
    p_run.add_argument(
        "--work-root",
        type=Path,
        required=True,
        help=(
            "Parent dir for the per-instance workspace. Workspace is left "
            "on disk after the run for inspection."
        ),
    )
    p_run.add_argument(
        "--api-key",
        default=None,
        help="OpenRouter API key. Defaults to $OPENROUTER_API_KEY.",
    )
    p_run.add_argument(
        "--model",
        required=True,
        help=(
            "LLM model ID (e.g. ``deepseek/deepseek-chat``). Mandatory — "
            "EVAL_BASELINE Standard 2 requires every report carries the "
            "model identity."
        ),
    )
    p_run.add_argument(
        "--remote-url",
        default=None,
        help="Override the clone URL. Useful for hermetic local mirrors.",
    )
    p_run.add_argument(
        "--arch",
        default=None,
        help="Image arch override (x86_64 / arm64).",
    )
    p_run.add_argument(
        "--max-steps",
        type=int,
        default=20,
        help="shell_agent max plan iterations.",
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
        if args.command == "run-agent":
            return _cmd_run_agent(args)
    except SWEBenchDatasetError as exc:
        print(f"eval.swebench: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except WorkspaceError as exc:
        print(f"eval.swebench: {exc}", file=sys.stderr)
        return EXIT_WORKSPACE
    except DockerError as exc:
        print(f"eval.swebench: {exc}", file=sys.stderr)
        return EXIT_DOCKER
    except EvalLLMConfigError as exc:
        print(f"eval.swebench: {exc}", file=sys.stderr)
        return EXIT_LLM_CONFIG
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
    dataset_path = Path(args.dataset) if args.dataset is not None else builtin_dataset_path()
    try:
        inst = load_instance(args.instance_id, dataset_path)
    except SWEBenchDatasetError as exc:
        print(f"eval.swebench: {exc}", file=sys.stderr)
        return EXIT_NOT_FOUND
    patch_text = _read_patch(args.patch)
    result = asyncio.run(
        evaluate_patch(
            inst,
            patch_text,
            arch=args.arch,
            test_output_path=args.log_test_output,
            dataset_snapshot=dataset_snapshot(dataset_path),
            harness_version=harness_version(),
        )
    )
    print(result.model_dump_json(indent=2))
    print(result.summary, file=sys.stderr)
    return EXIT_OK if result.resolved else EXIT_NOT_RESOLVED


def _read_patch(path: Path) -> str:
    if str(path) == "-":
        return sys.stdin.read()
    return path.read_text(encoding="utf-8")


def _cmd_run_agent(args: argparse.Namespace) -> int:
    dataset_path = Path(args.dataset) if args.dataset is not None else builtin_dataset_path()
    try:
        inst = load_instance(args.instance_id, dataset_path)
    except SWEBenchDatasetError as exc:
        print(f"eval.swebench: {exc}", file=sys.stderr)
        return EXIT_NOT_FOUND
    llm = build_default_llm(api_key=args.api_key, default_model=args.model)
    eval_result, agent_result = asyncio.run(
        run_full_pipeline(
            inst,
            llm=llm,
            work_root=args.work_root,
            remote_url=args.remote_url,
            arch=args.arch,
            max_steps=args.max_steps,
            dataset_snapshot=dataset_snapshot(dataset_path),
            harness_version=harness_version(),
            model=args.model,
            prompt_version=PROMPT_VERSION,
        )
    )
    print(eval_result.model_dump_json(indent=2))
    print(
        f"{eval_result.summary} (agent took {agent_result.steps} steps, "
        f"patch={len(agent_result.patch)} bytes)",
        file=sys.stderr,
    )
    return EXIT_OK if eval_result.resolved else EXIT_NOT_RESOLVED


if __name__ == "__main__":  # pragma: no cover - executed via python -m
    sys.exit(main())


__all__ = ["EXIT_NOT_FOUND", "EXIT_OK", "EXIT_USAGE", "build_parser", "main"]
