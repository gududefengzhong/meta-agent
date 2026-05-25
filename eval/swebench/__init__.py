"""SWE-bench harness.

Scope contract: ``docs/specs/EVAL_BASELINE.md``. The harness drives
single-instance evaluation; supports two modes:

* **Gold / supplied patch**: caller hands a patch text to
  :func:`evaluate_patch`; harness applies it, runs tests, scores.
  ``model`` / ``prompt_version`` left ``None`` on the result.
* **Agent-produced patch**: :func:`run_full_pipeline` clones the
  repo, drives ``builtin.shell_agent`` against the instance,
  extracts the agent's diff, scores. ``model`` /
  ``prompt_version`` stamped onto the result for replay
  identification.

Re-exported symbols (the public surface):

* :class:`SWEBenchInstance` — domain model for one dataset row
* :func:`load_instances` / :func:`load_instance` — dataset loader
* :func:`image_name_for_instance` — per-instance eval image name
* :class:`Container` / :func:`ensure_image_pulled` — container layer
* :func:`evaluate_patch` — apply a patch + score it
* :func:`run_full_pipeline` / :class:`AgentRunResult` — agent
  pipeline (clone → agent → score)
* :func:`prepare_workspace` / :func:`apply_test_patch` / :func:`extract_patch`
* :func:`build_default_llm` — OpenRouter LLM client builder
* :class:`InstanceResult` / :class:`TestSelectorResult` — result types
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
from eval.swebench.patches import apply_test_patch, extract_patch
from eval.swebench.pipeline import run_full_pipeline
from eval.swebench.results import (
    InstanceReport,
    InstanceResult,
    TestSelectorResult,
    TestStatus,
)
from eval.swebench.workspace import WorkspaceError, prepare_workspace

__all__ = [
    "DEFAULT_IMAGE_REGISTRY",
    "AgentRunResult",
    "Container",
    "DockerError",
    "EvalLLMConfigError",
    "InstanceReport",
    "InstanceResult",
    "SWEBenchInstance",
    "TestSelectorResult",
    "TestStatus",
    "WorkspaceError",
    "apply_test_patch",
    "build_default_llm",
    "ensure_image_pulled",
    "evaluate_patch",
    "extract_patch",
    "image_name_for_instance",
    "load_instance",
    "load_instances",
    "normalize_instance_id",
    "prepare_workspace",
    "run_agent_on_instance",
    "run_full_pipeline",
]
