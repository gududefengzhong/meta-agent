"""Unit tests for :mod:`eval.swebench.identity`.

These guard the EVAL_BASELINE Standard 1 + Standard 2 contract
that every report carries identity fields whose values are
stable across runs of the same input.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from eval.swebench.identity import dataset_snapshot, harness_version

# --------------------------------------------------------------- dataset_snapshot


def test_dataset_snapshot_is_stable_for_unchanged_file(tmp_path: Path) -> None:
    """Two calls with the same file content must produce the same hash."""

    path = tmp_path / "dataset.json"
    path.write_text('[{"instance_id": "x"}]', encoding="utf-8")
    assert dataset_snapshot(path) == dataset_snapshot(path)


def test_dataset_snapshot_changes_with_content(tmp_path: Path) -> None:
    """Different content → different hash (the whole point)."""

    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text("[]", encoding="utf-8")
    b.write_text("[{}]", encoding="utf-8")
    assert dataset_snapshot(a) != dataset_snapshot(b)


def test_dataset_snapshot_is_12_chars_lowercase_hex(tmp_path: Path) -> None:
    """Standard 1 specifies SHA-256[:12]; pin the shape so callers
    can compare values byte-for-byte across reports."""

    path = tmp_path / "x.json"
    path.write_text("contents", encoding="utf-8")
    snap = dataset_snapshot(path)
    assert len(snap) == 12
    assert snap == snap.lower()
    assert all(c in "0123456789abcdef" for c in snap)


def test_dataset_snapshot_raises_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        dataset_snapshot(tmp_path / "does-not-exist.json")


# --------------------------------------------------------------- harness_version


def test_harness_version_returns_a_short_string() -> None:
    """Returns the git short SHA when inside a git checkout; the
    test env is the meta-agent repo so that's the typical case."""

    v = harness_version()
    assert isinstance(v, str)
    assert v  # never empty


def test_harness_version_falls_back_when_git_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``"unknown"`` rather than crashing when git isn't on PATH."""

    import subprocess

    # Bust the lru_cache so the monkeypatch takes effect for this
    # one assertion. The cache is helpful in production but breaks
    # test isolation.
    harness_version.cache_clear()

    def _raise(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("simulated missing git")

    monkeypatch.setattr(subprocess, "run", _raise)
    try:
        assert harness_version() == "unknown"
    finally:
        harness_version.cache_clear()
