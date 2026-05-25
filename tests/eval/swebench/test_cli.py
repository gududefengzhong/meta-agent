"""Unit tests for the ``python -m eval.swebench`` CLI.

Phase-1 scope: ``list`` / ``show`` / ``evaluate`` only. The
``prepare`` / ``diff`` / ``run-agent`` / ``run-batch`` /
``score-gold-batch`` subcommands are deferred along with their
underlying modules (``workspace.py`` / ``patches.py`` / ``agent.py``
/ ``pipeline.py`` / ``batch.py`` / ``llm_factory.py``). They come
back in later PRs.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from eval.swebench.__main__ import EXIT_NOT_FOUND, EXIT_OK, main


def _write_local_dataset(tmp_path: Path, *, base_commit: str, instance_id: str) -> Path:
    # ``psf/requests`` v2.4 is in the Phase-1 whitelist, so the
    # evaluate CLI can run end-to-end against this fake dataset.
    rows = [
        {
            "instance_id": instance_id,
            "repo": "psf/requests",
            "base_commit": base_commit,
            "problem_statement": "fix the bug",
            "patch": "",
            "test_patch": "",
            "FAIL_TO_PASS": ["test_requests.py::TestRequests::test_marker"],
            "PASS_TO_PASS": [],
            "version": "2.4",
            "environment_setup_commit": base_commit,
        }
    ]
    path = tmp_path / "instances.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


# ----------------------------------------------------------------- list / show


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


# ----------------------------------------------------------------- evaluate


def _script_docker(monkeypatch: pytest.MonkeyPatch, responses: list[tuple[int, str, str]]) -> None:
    """Inject a scripted ``_docker_run`` that emits ``responses`` in order."""

    from eval.swebench.containers import DockerError

    from eval.swebench import containers

    script = iter(responses)

    def fake(
        cmd: Sequence[str],
        *,
        what: str,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            rc, out, err = next(script)
        except StopIteration as exc:
            raise AssertionError(f"docker script exhausted at {what!r}") from exc
        if check and rc != 0:
            raise DockerError(f"{what} failed: {err or out}")
        return subprocess.CompletedProcess(cmd, rc, out, err)

    monkeypatch.setattr(containers, "_docker_run", fake)


def test_evaluate_resolved_exits_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A resolved evaluation prints the InstanceResult JSON + summary, exits 0."""

    _script_docker(
        monkeypatch,
        [
            (0, "", ""),  # docker image inspect → cached
            (0, "", ""),  # docker run
            # No test_patch on this fake instance (test_patch is empty
            # string), so no test_patch git-apply exec.
            (0, "", ""),  # exec: git apply agent patch
            (0, "PASSED test_requests.py::TestRequests::test_marker\n", ""),  # exec: pytest
            (0, "", ""),  # docker rm
        ],
    )
    dataset = _write_local_dataset(tmp_path, base_commit="abc123", instance_id="test__repo-1")
    patch_file = tmp_path / "agent.patch"
    patch_file.write_text("diff --git a/x b/x\n")

    code = main(
        [
            "--dataset",
            str(dataset),
            "evaluate",
            "test__repo-1",
            "--patch",
            str(patch_file),
        ]
    )
    assert code == EXIT_OK
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload["instance_id"] == "test__repo-1"
    assert "RESOLVED" in out.err


def test_evaluate_not_resolved_exits_5(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from eval.swebench.__main__ import EXIT_NOT_RESOLVED

    _script_docker(
        monkeypatch,
        [
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (1, "FAILED test_requests.py::TestRequests::test_marker - AssertionError\n", ""),
            (0, "", ""),
        ],
    )
    dataset = _write_local_dataset(tmp_path, base_commit="abc123", instance_id="test__repo-1")
    patch_file = tmp_path / "agent.patch"
    patch_file.write_text("diff --git a/x b/x\n")

    code = main(
        [
            "--dataset",
            str(dataset),
            "evaluate",
            "test__repo-1",
            "--patch",
            str(patch_file),
        ]
    )
    assert code == EXIT_NOT_RESOLVED
    assert "FAILED" in capsys.readouterr().err


def test_evaluate_unknown_instance_id_exits_3(
    tmp_path: Path,
) -> None:
    dataset = _write_local_dataset(tmp_path, base_commit="abc123", instance_id="test__repo-1")
    patch_file = tmp_path / "agent.patch"
    patch_file.write_text("diff\n")

    code = main(
        [
            "--dataset",
            str(dataset),
            "evaluate",
            "nonexistent",
            "--patch",
            str(patch_file),
        ]
    )
    assert code == EXIT_NOT_FOUND
