"""Unit tests for :mod:`eval.swebench.images`."""

from __future__ import annotations

import pytest
from eval.swebench.images import (
    DEFAULT_IMAGE_REGISTRY,
    image_name_for_instance,
    normalize_instance_id,
)
from eval.swebench.instances import SWEBenchInstance


def _instance(instance_id: str = "django__django-13768") -> SWEBenchInstance:
    return SWEBenchInstance(
        instance_id=instance_id,
        repo="django/django",
        base_commit="0c42cdf0",
    )


def test_normalize_replaces_org_repo_separator() -> None:
    assert normalize_instance_id("django__django-13768") == "django_1776_django-13768"


def test_normalize_lowercases_input() -> None:
    assert normalize_instance_id("SymPy__Sympy-123") == "sympy_1776_sympy-123"


def test_normalize_rejects_empty_id() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        normalize_instance_id("")


def test_image_name_uses_normalized_id() -> None:
    inst = _instance("django__django-13768")
    name = image_name_for_instance(inst, arch="x86_64")
    assert name == "swebench/sweb.eval.x86_64.django_1776_django-13768:latest"


def test_image_name_explicit_arch_overrides_detection() -> None:
    inst = _instance("django__django-13768")
    x86 = image_name_for_instance(inst, arch="x86_64")
    arm = image_name_for_instance(inst, arch="arm64")
    assert ".x86_64." in x86
    assert ".arm64." in arm


def test_image_name_custom_registry_and_tag() -> None:
    inst = _instance("django__django-13768")
    name = image_name_for_instance(inst, arch="x86_64", registry="my.mirror/swe", tag="v2")
    assert name == "my.mirror/swe/sweb.eval.x86_64.django_1776_django-13768:v2"


def test_image_name_rejects_empty_registry() -> None:
    inst = _instance()
    with pytest.raises(ValueError, match="registry"):
        image_name_for_instance(inst, registry="")


def test_image_name_rejects_unsupported_arch() -> None:
    inst = _instance()
    with pytest.raises(ValueError, match="unsupported arch"):
        image_name_for_instance(inst, arch="riscv")


def test_default_registry_constant_matches_official() -> None:
    assert DEFAULT_IMAGE_REGISTRY == "swebench"
