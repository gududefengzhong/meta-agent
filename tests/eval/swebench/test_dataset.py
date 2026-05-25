"""Unit tests for :mod:`eval.swebench.dataset`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from eval.swebench.dataset import (
    SWEBenchDatasetError,
    load_instance,
    load_instances,
)


def _write_dataset(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    path = tmp_path / "instances.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def _valid_row(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "instance_id": "org__repo-1",
        "repo": "org/repo",
        "base_commit": "abcdef",
        "problem_statement": "fix the bug",
        "patch": "diff --git a/x b/x",
        "test_patch": "",
        "FAIL_TO_PASS": ["tests/test_x.py::test_a"],
        "PASS_TO_PASS": ["tests/test_x.py::test_b"],
        "version": "1.0",
        "environment_setup_commit": "abcdef",
    }
    base.update(overrides)
    return base


def test_load_instances_parses_valid_row(tmp_path: Path) -> None:
    path = _write_dataset(tmp_path, [_valid_row()])
    instances = load_instances(path)
    assert len(instances) == 1
    inst = instances[0]
    assert inst.instance_id == "org__repo-1"
    assert inst.fail_to_pass == ("tests/test_x.py::test_a",)
    assert inst.pass_to_pass == ("tests/test_x.py::test_b",)


def test_load_instances_accepts_json_encoded_test_selectors(tmp_path: Path) -> None:
    """HuggingFace sometimes ships the list columns as JSON strings."""

    row = _valid_row(
        FAIL_TO_PASS=json.dumps(["tests/test_x.py::test_a"]),
        PASS_TO_PASS=json.dumps([]),
    )
    path = _write_dataset(tmp_path, [row])
    instances = load_instances(path)
    assert instances[0].fail_to_pass == ("tests/test_x.py::test_a",)
    assert instances[0].pass_to_pass == ()


def test_load_instances_preserves_unknown_columns_in_extra(tmp_path: Path) -> None:
    row = _valid_row(hints_text="look at the slots", created_at="2024-01-01")
    path = _write_dataset(tmp_path, [row])
    inst = load_instances(path)[0]
    assert inst.extra == {"hints_text": "look at the slots", "created_at": "2024-01-01"}


def test_load_instances_filters_by_repo(tmp_path: Path) -> None:
    rows = [
        _valid_row(instance_id="org__repo-1", repo="org/repo"),
        _valid_row(instance_id="other__pkg-2", repo="other/pkg"),
    ]
    path = _write_dataset(tmp_path, rows)
    only_org = load_instances(path, repos={"org/repo"})
    assert [i.instance_id for i in only_org] == ["org__repo-1"]


def test_load_instances_respects_limit(tmp_path: Path) -> None:
    rows = [_valid_row(instance_id=f"org__repo-{i}") for i in range(5)]
    path = _write_dataset(tmp_path, rows)
    out = load_instances(path, limit=2)
    assert [i.instance_id for i in out] == ["org__repo-0", "org__repo-1"]


def test_load_instances_negative_limit_rejected(tmp_path: Path) -> None:
    path = _write_dataset(tmp_path, [_valid_row()])
    with pytest.raises(SWEBenchDatasetError, match="limit must be >= 0"):
        load_instances(path, limit=-1)


def test_load_instances_missing_file_raises() -> None:
    with pytest.raises(SWEBenchDatasetError, match="not found"):
        load_instances(Path("/definitely/not/there.json"))


def test_load_instances_top_level_must_be_array(tmp_path: Path) -> None:
    path = tmp_path / "instances.json"
    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(SWEBenchDatasetError, match="JSON array"):
        load_instances(path)


def test_load_instances_invalid_row_raises(tmp_path: Path) -> None:
    # ``repo`` is required (min_length=1); empty string violates the schema.
    row = _valid_row(repo="")
    path = _write_dataset(tmp_path, [row])
    with pytest.raises(SWEBenchDatasetError, match="failed validation"):
        load_instances(path)


def test_load_instances_test_selectors_unparsable_raises(tmp_path: Path) -> None:
    row = _valid_row(FAIL_TO_PASS="not json at all")
    path = _write_dataset(tmp_path, [row])
    with pytest.raises(SWEBenchDatasetError):
        load_instances(path)


def test_load_instance_returns_matching_row(tmp_path: Path) -> None:
    rows = [_valid_row(instance_id=f"org__repo-{i}") for i in range(3)]
    path = _write_dataset(tmp_path, rows)
    inst = load_instance("org__repo-2", path)
    assert inst.instance_id == "org__repo-2"


def test_load_instance_missing_id_raises(tmp_path: Path) -> None:
    path = _write_dataset(tmp_path, [_valid_row()])
    with pytest.raises(SWEBenchDatasetError, match="not found in dataset"):
        load_instance("nonexistent", path)


def test_builtin_fixture_loads() -> None:
    """The checked-in fixture parses + validates cleanly."""

    instances = load_instances()
    assert len(instances) >= 1
    assert any(inst.repo == "django/django" for inst in instances)
