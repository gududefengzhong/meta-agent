"""Resolve the per-instance evaluation Docker image name.

SWE-bench publishes one prebuilt evaluation image per instance, so
a harness can launch the test environment without provisioning
the repo / Python deps itself. The image name follows a strict
convention; we re-implement it here so we don't take a runtime
dependency on the ``swebench`` PyPI package (it ships a heavy
test-runner stack we don't want).

Naming convention (matches the upstream harness as of 2024-Q3+)
==============================================================
::

    {registry}/sweb.eval.{arch}.{normalized_instance_id}:latest

Where:

* ``registry`` is the Docker registry namespace (``swebench`` is
  the official one; configurable here for mirrors / private
  rebuilds).
* ``arch`` is ``x86_64`` or ``arm64`` — chosen from the running
  process's CPU arch (or pinned via :func:`image_name_for_instance`'s
  ``arch`` kwarg for explicit cross-build flows).
* ``normalized_instance_id`` lowercases the ``instance_id`` and
  rewrites ``__`` (the org/repo separator in instance ids) to
  ``_1776_`` — the upstream registry can't host ``__`` so they
  encode it with a tag that won't collide with anything in real
  instance names.

The arch defaults are intentional: SWE-bench's prebuilt images
exist primarily on x86_64. ARM64 builds are sparser; the resolver
still produces the canonical name but callers should expect more
pull failures and may need to swap to ``x86_64`` via emulation.
"""

from __future__ import annotations

import platform

from eval.swebench.instances import SWEBenchInstance

DEFAULT_IMAGE_REGISTRY = "swebench"
_INSTANCE_SEPARATOR = "__"
_NORMALIZED_SEPARATOR = "_1776_"

_SUPPORTED_ARCHES = frozenset({"x86_64", "arm64"})


def image_name_for_instance(
    instance: SWEBenchInstance,
    *,
    registry: str = DEFAULT_IMAGE_REGISTRY,
    arch: str | None = None,
    tag: str = "latest",
) -> str:
    """Return the canonical evaluation image name for ``instance``.

    The resolver does NOT check whether the image actually exists
    on the registry — that's a follow-up concern (image pull /
    cache management is PR 2). This function is pure string
    manipulation so it can run anywhere.
    """

    if not registry:
        raise ValueError("registry must be a non-empty string")
    resolved_arch = arch if arch is not None else _detect_arch()
    if resolved_arch not in _SUPPORTED_ARCHES:
        raise ValueError(
            f"unsupported arch {resolved_arch!r}; expected one of {sorted(_SUPPORTED_ARCHES)}"
        )
    normalized = normalize_instance_id(instance.instance_id)
    return f"{registry}/sweb.eval.{resolved_arch}.{normalized}:{tag}"


def normalize_instance_id(instance_id: str) -> str:
    """Encode an instance_id for use as a Docker image tag.

    Lowercases and rewrites the ``__`` org/repo separator to the
    sentinel ``_1776_`` (matches the upstream SWE-bench harness).
    Other characters pass through unchanged.
    """

    if not instance_id:
        raise ValueError("instance_id must be a non-empty string")
    return instance_id.lower().replace(_INSTANCE_SEPARATOR, _NORMALIZED_SEPARATOR)


def _detect_arch() -> str:
    """Best-effort current-process arch detection.

    ``platform.machine()`` returns architecture-flavoured strings
    (``"x86_64"``, ``"AMD64"``, ``"arm64"``, ``"aarch64"``). Map
    them onto the two SWE-bench-supported arches; unknown values
    fall back to ``"x86_64"`` because that's where the largest
    image catalogue lives.
    """

    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "x86_64"
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    return "x86_64"


__all__ = [
    "DEFAULT_IMAGE_REGISTRY",
    "image_name_for_instance",
    "normalize_instance_id",
]
