"""SWE-bench harness (Track B).

PR layout
=========
* PR #42 — dataset loader + image name resolver + inventory CLI
* PR #43 — workspace clone + test_patch apply + diff extract
* PR #44 — Docker isolation + patch scorer
* PR #46 — meta-agent ↔ eval bridge + ``run-agent`` CLI
* PR #47 — batch runner + ``run-batch`` CLI + ``BatchReport`` with pass@1

Public surface mirrors the PR order; everything that downstream
callers actually need is re-exported here so users only see
``eval.swebench`` in their imports.
"""

from eval.swebench.agent import AgentRunResult, run_agent_on_instance
from eval.swebench.batch import run_batch
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
from eval.swebench.results import (
    BatchReport,
    InstanceReport,
    InstanceResult,
    TestSelectorResult,
    TestStatus,
)

__all__ = [
    "DEFAULT_IMAGE_REGISTRY",
    "AgentRunResult",
    "BatchReport",
    "Container",
    "DockerError",
    "EvalLLMConfigError",
    "InstanceReport",
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
    "run_batch",
    "run_full_pipeline",
]
