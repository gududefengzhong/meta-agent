"""SWE-bench harness — Phase-1 (post-revert restoration).

Scope contract: ``docs/specs/EVAL_BASELINE.md``. Phase-1 surface
is **read-only data + single-instance evaluation against a
supplied patch**. Batch runner / agent driver / prediction
pipeline come back in later phases once the per-instance path is
proven stable end-to-end against real eval images.

Re-exported symbols:

* :class:`SWEBenchInstance` — domain model for one dataset row
* :func:`load_instances` / :func:`load_instance` — dataset loader
* :func:`image_name_for_instance` — per-instance eval image name
* :class:`Container` / :func:`ensure_image_pulled` — container layer
* :func:`evaluate_patch` — apply a patch + score it
* :class:`InstanceResult` / :class:`TestSelectorResult` — result types
"""

from eval.swebench.containers import Container, DockerError, ensure_image_pulled
from eval.swebench.dataset import load_instance, load_instances
from eval.swebench.evaluate import evaluate_patch
from eval.swebench.images import (
    DEFAULT_IMAGE_REGISTRY,
    image_name_for_instance,
    normalize_instance_id,
)
from eval.swebench.instances import SWEBenchInstance
from eval.swebench.results import (
    InstanceReport,
    InstanceResult,
    TestSelectorResult,
    TestStatus,
)

__all__ = [
    "DEFAULT_IMAGE_REGISTRY",
    "Container",
    "DockerError",
    "InstanceReport",
    "InstanceResult",
    "SWEBenchInstance",
    "TestSelectorResult",
    "TestStatus",
    "ensure_image_pulled",
    "evaluate_patch",
    "image_name_for_instance",
    "load_instance",
    "load_instances",
    "normalize_instance_id",
]
