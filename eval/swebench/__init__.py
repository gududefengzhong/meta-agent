"""SWE-bench harness (Track B, PR 1: scaffold).

What ships in this PR
=====================
* :class:`SWEBenchInstance` — domain model for one row of the
  SWE-bench dataset (instance_id, repo, base_commit, problem
  statement, gold patch, test selectors, env metadata).
* :func:`load_instances` / :func:`load_instance` — JSON-file
  loader. v0 reads from a checked-in fixture so the inventory
  works offline; a HuggingFace ``datasets`` integration is the
  next PR.
* :func:`image_name_for_instance` — resolves the per-instance
  evaluation Docker image name from the SWE-bench convention.
* CLI ``python -m eval.swebench`` — ``list`` + ``show <id>``
  inspection commands.

What this PR deliberately does NOT do
=====================================
* Pull Docker images or execute anything inside them
* Submit tasks to the meta-agent API
* Run the test selectors (FAIL_TO_PASS / PASS_TO_PASS)
* Compute pass rates

Those land in PR 2 (agent + patch extraction) and PR 3 (test
execution + result aggregation + CI gates).
"""

from eval.swebench.agent import AgentRunResult, run_agent_on_instance
from eval.swebench.containers import Container, DockerError, ensure_image_pulled
from eval.swebench.dataset import load_instance, load_instances
from eval.swebench.evaluate import evaluate_patch
from eval.swebench.images import (
    DEFAULT_IMAGE_REGISTRY,
    image_name_for_instance,
    normalize_instance_id,
)
from eval.swebench.instances import SWEBenchInstance
from eval.swebench.llm_factory import EvalLLMConfigError, build_default_llm
from eval.swebench.pipeline import run_full_pipeline
from eval.swebench.results import InstanceResult, TestSelectorResult, TestStatus

__all__ = [
    "DEFAULT_IMAGE_REGISTRY",
    "AgentRunResult",
    "Container",
    "DockerError",
    "EvalLLMConfigError",
    "InstanceResult",
    "SWEBenchInstance",
    "TestSelectorResult",
    "TestStatus",
    "build_default_llm",
    "ensure_image_pulled",
    "evaluate_patch",
    "image_name_for_instance",
    "load_instance",
    "load_instances",
    "normalize_instance_id",
    "run_agent_on_instance",
    "run_full_pipeline",
]
