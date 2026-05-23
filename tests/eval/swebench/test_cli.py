"""Unit tests for the ``python -m eval.swebench`` CLI."""

from __future__ import annotations

import json

import pytest
from eval.swebench.__main__ import EXIT_NOT_FOUND, EXIT_OK, main


def test_list_prints_built_in_fixture(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["list"])
    assert code == EXIT_OK
    out = capsys.readouterr()
    assert "django__django-13768" in out.out
    # Header is on stderr so stdout pipes carry only data rows.
    assert "instance_id\trepo\tversion\timage" in out.err


def test_list_repo_filter(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["list", "--repo", "psf/requests"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "psf__requests-2317" in out
    assert "django__django-13768" not in out


def test_list_limit_caps_rows(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["list", "--limit", "1"])
    assert code == EXIT_OK
    rows = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(rows) == 1


def test_list_empty_filter_prints_friendly_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["list", "--repo", "nope/nothing"])
    assert code == EXIT_OK
    out = capsys.readouterr()
    assert "(no instances matched)" in out.err


def test_show_emits_full_instance_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["show", "django__django-13768"])
    assert code == EXIT_OK
    decoded = json.loads(capsys.readouterr().out)
    assert decoded["instance_id"] == "django__django-13768"
    assert "image" in decoded
    assert decoded["image"].startswith("swebench/sweb.eval.")


def test_show_missing_instance_exits_3(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["show", "definitely-not-there"])
    assert code == EXIT_NOT_FOUND


def test_missing_command_rejected_by_parser() -> None:
    with pytest.raises(SystemExit):
        main([])
