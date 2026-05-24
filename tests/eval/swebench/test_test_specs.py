"""Unit tests for :mod:`eval.swebench.test_specs`."""

from __future__ import annotations

import pytest
from eval.swebench.instances import SWEBenchInstance
from eval.swebench.test_specs import TestSpecNotFoundError, spec_for


def _instance(*, repo: str, version: str) -> SWEBenchInstance:
    return SWEBenchInstance(
        instance_id=f"{repo.replace('/', '__')}-1",
        repo=repo,
        base_commit="abc",
        version=version,
    )


def test_spec_for_django_3_2_uses_django_runner_and_parser() -> None:
    spec = spec_for(_instance(repo="django/django", version="3.2"))
    assert "runtests.py" in spec.test_cmd
    assert spec.parser == "django"


def test_spec_for_sympy_1_8_uses_sympy_runner_and_parser() -> None:
    spec = spec_for(_instance(repo="sympy/sympy", version="1.8"))
    assert "bin/test" in spec.test_cmd
    assert spec.parser == "sympy"


def test_spec_for_psf_requests_2_5_uses_pytest_options() -> None:
    spec = spec_for(_instance(repo="psf/requests", version="2.5"))
    assert "pytest" in spec.test_cmd
    assert spec.parser == "pytest_options"


def test_spec_for_unknown_repo_raises_with_clear_error() -> None:
    with pytest.raises(TestSpecNotFoundError, match="no test spec"):
        spec_for(_instance(repo="unknown/repo", version="1.0"))


def test_spec_for_unknown_version_raises() -> None:
    # Known repo but unknown version is still a miss — versions
    # diverge significantly across SWE-bench releases.
    with pytest.raises(TestSpecNotFoundError):
        spec_for(_instance(repo="django/django", version="0.1"))
