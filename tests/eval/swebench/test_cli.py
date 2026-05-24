"""Unit tests for the ``python -m eval.swebench`` CLI."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from eval.swebench.__main__ import EXIT_NOT_FOUND, EXIT_OK, EXIT_WORKSPACE, main


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _make_local_repo(tmp_path: Path) -> tuple[str, str, str]:
    """Build a bare repo and capture the head SHA. Returns (url, sha, repo).

    URL is returned as a string ``file://...`` because ``Path`` would
    normalise the double slash and break ``git clone``.
    """
    bare = tmp_path / "remote.git"
    work = tmp_path / "seed"
    bare.mkdir()
    work.mkdir()
    _git(bare, "init", "--bare", "--quiet")
    _git(work, "init", "--quiet", "--initial-branch=main")
    _git(work, "config", "user.email", "a@b")
    _git(work, "config", "user.name", "a")
    (work / "calc.py").write_text("def add(x, y):\n    return x + y\n")
    _git(work, "add", "calc.py")
    _git(work, "commit", "-q", "-m", "initial")
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work, text=True).strip()
    url = f"file://{bare}"
    _git(work, "remote", "add", "origin", url)
    _git(work, "push", "-q", "origin", "main")
    return url, sha, "test/repo"


def _write_local_dataset(tmp_path: Path, *, base_commit: str, instance_id: str) -> Path:
    # ``psf/requests`` v2.5 is registered in the test-spec table, so the
    # evaluate CLI can run end-to-end against this fake dataset. The
    # test_patch still edits ``calc.py`` (the seed repo's only file)
    # so the ``prepare --apply-test-patch`` test path keeps working
    # — the FAIL_TO_PASS selector and the test_patch target file
    # don't have to be the same file for these unit tests.
    rows = [
        {
            "instance_id": instance_id,
            "repo": "psf/requests",
            "base_commit": base_commit,
            "problem_statement": "fix the bug",
            "patch": "",
            "test_patch": (
                "diff --git a/calc.py b/calc.py\n"
                "--- a/calc.py\n"
                "+++ b/calc.py\n"
                "@@ -1,2 +1,2 @@\n"
                " def add(x, y):\n"
                "-    return x + y\n"
                "+    return x + y  # test marker\n"
            ),
            "FAIL_TO_PASS": ["test_requests.py::TestRequests::test_marker"],
            "PASS_TO_PASS": [],
            "version": "2.5",
            "environment_setup_commit": base_commit,
        }
    ]
    path = tmp_path / "instances.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


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


# ---------------------------------------------------- prepare command


def test_prepare_clones_workspace_and_prints_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url, sha, _repo = _make_local_repo(tmp_path)
    dataset = _write_local_dataset(tmp_path, base_commit=sha, instance_id="test__repo-1")
    workspace = tmp_path / "ws"
    code = main(
        [
            "--dataset",
            str(dataset),
            "prepare",
            "test__repo-1",
            "--out",
            str(workspace),
            "--remote-url",
            url,
        ]
    )
    assert code == EXIT_OK
    out = capsys.readouterr()
    assert str(workspace.resolve()) in out.out
    assert "prepared test__repo-1" in out.err
    assert (workspace / "calc.py").exists()


def test_prepare_apply_test_patch_lands_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url, sha, _repo = _make_local_repo(tmp_path)
    dataset = _write_local_dataset(tmp_path, base_commit=sha, instance_id="test__repo-1")
    workspace = tmp_path / "ws"
    code = main(
        [
            "--dataset",
            str(dataset),
            "prepare",
            "test__repo-1",
            "--out",
            str(workspace),
            "--remote-url",
            url,
            "--apply-test-patch",
        ]
    )
    assert code == EXIT_OK
    assert "# test marker" in (workspace / "calc.py").read_text()


def test_prepare_unknown_instance_id_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url, sha, _repo = _make_local_repo(tmp_path)
    dataset = _write_local_dataset(tmp_path, base_commit=sha, instance_id="test__repo-1")
    workspace = tmp_path / "ws"
    code = main(
        [
            "--dataset",
            str(dataset),
            "prepare",
            "nonexistent",
            "--out",
            str(workspace),
            "--remote-url",
            url,
        ]
    )
    assert code == EXIT_NOT_FOUND


def test_prepare_existing_dest_exits_4(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    url, sha, _repo = _make_local_repo(tmp_path)
    dataset = _write_local_dataset(tmp_path, base_commit=sha, instance_id="test__repo-1")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "junk").write_text("x")
    code = main(
        [
            "--dataset",
            str(dataset),
            "prepare",
            "test__repo-1",
            "--out",
            str(workspace),
            "--remote-url",
            url,
        ]
    )
    assert code == EXIT_WORKSPACE


# ---------------------------------------------------- diff command


def test_diff_with_instance_id_uses_dataset_base_commit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url, sha, _repo = _make_local_repo(tmp_path)
    dataset = _write_local_dataset(tmp_path, base_commit=sha, instance_id="test__repo-1")
    workspace = tmp_path / "ws"
    main(
        [
            "--dataset",
            str(dataset),
            "prepare",
            "test__repo-1",
            "--out",
            str(workspace),
            "--remote-url",
            url,
        ]
    )
    # Mutate the workspace
    (workspace / "calc.py").write_text("def add(x, y):\n    return x + y + 42\n")
    capsys.readouterr()  # drop prior output
    code = main(
        [
            "--dataset",
            str(dataset),
            "diff",
            str(workspace),
            "--instance-id",
            "test__repo-1",
        ]
    )
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "+    return x + y + 42" in out


def test_diff_with_explicit_base_commit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    url, sha, _repo = _make_local_repo(tmp_path)
    dataset = _write_local_dataset(tmp_path, base_commit=sha, instance_id="test__repo-1")
    workspace = tmp_path / "ws"
    main(
        [
            "--dataset",
            str(dataset),
            "prepare",
            "test__repo-1",
            "--out",
            str(workspace),
            "--remote-url",
            url,
        ]
    )
    capsys.readouterr()  # drop prior output
    code = main(["diff", str(workspace), "--base-commit", sha])
    assert code == EXIT_OK
    # No mutation: diff is empty.
    assert capsys.readouterr().out == ""


def test_diff_requires_one_of_instance_id_or_base_commit(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    with pytest.raises(SystemExit):
        main(["diff", str(workspace)])


# ---------------------------------------------------- evaluate command


def _script_docker(monkeypatch: pytest.MonkeyPatch, responses: list[tuple[int, str, str]]) -> None:
    """Inject a scripted ``_docker_run`` that emits ``responses`` in order."""

    from eval.swebench.containers import DockerError

    from eval.swebench import containers

    script = iter(responses)

    def fake(
        cmd: list[str],
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

    from eval.swebench.__main__ import EXIT_OK

    _script_docker(
        monkeypatch,
        [
            (0, "", ""),  # docker image inspect → cached
            (0, "", ""),  # docker run
            (0, "", ""),  # exec: git apply test_patch
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


# --------------------------------------------------- score-gold-batch command


def _write_dataset_with_gold(
    tmp_path: Path,
    *,
    instance_id: str,
    gold_patch: str,
) -> Path:
    rows = [
        {
            "instance_id": instance_id,
            "repo": "psf/requests",
            "base_commit": "abc123",
            "problem_statement": "fix the bug",
            "patch": gold_patch,
            "test_patch": "",
            "FAIL_TO_PASS": ["test_requests.py::TestRequests::test_marker"],
            "PASS_TO_PASS": [],
            "version": "2.5",
            "environment_setup_commit": "abc123",
        }
    ]
    path = tmp_path / "instances.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def test_score_gold_batch_resolved_exits_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Gold patch that resolves leaves the gate green (exit 0)."""

    _script_docker(
        monkeypatch,
        [
            (0, "", ""),  # docker image inspect
            (0, "", ""),  # docker run
            (0, "", ""),  # exec: git apply gold patch (test_patch is empty)
            (0, "PASSED test_requests.py::TestRequests::test_marker\n", ""),  # pytest
            (0, "", ""),  # docker rm
        ],
    )
    dataset = _write_dataset_with_gold(
        tmp_path,
        instance_id="test__repo-1",
        gold_patch="diff --git a/x b/x\n",
    )
    code = main(
        [
            "--dataset",
            str(dataset),
            "score-gold-batch",
            "--instance-ids",
            "test__repo-1",
        ]
    )
    assert code == EXIT_OK
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload["total"] == 1
    assert payload["resolved"] == 1


def test_score_gold_batch_default_fail_under_is_strict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing gold patch trips the default ``--fail-under 1.0`` gate."""

    from eval.swebench.__main__ import EXIT_NOT_RESOLVED

    _script_docker(
        monkeypatch,
        [
            (0, "", ""),  # inspect
            (0, "", ""),  # run
            (0, "", ""),  # apply gold
            (
                1,
                "FAILED test_requests.py::TestRequests::test_marker - AssertionError\n",
                "",
            ),  # pytest
            (0, "", ""),  # rm
        ],
    )
    dataset = _write_dataset_with_gold(
        tmp_path,
        instance_id="test__repo-1",
        gold_patch="diff --git a/x b/x\n",
    )
    code = main(
        [
            "--dataset",
            str(dataset),
            "score-gold-batch",
            "--instance-ids",
            "test__repo-1",
        ]
    )
    assert code == EXIT_NOT_RESOLVED


def test_score_gold_batch_writes_report_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _script_docker(
        monkeypatch,
        [
            (0, "", ""),
            (0, "", ""),
            (0, "", ""),
            (0, "PASSED test_requests.py::TestRequests::test_marker\n", ""),
            (0, "", ""),
        ],
    )
    dataset = _write_dataset_with_gold(
        tmp_path,
        instance_id="test__repo-1",
        gold_patch="diff --git a/x b/x\n",
    )
    report_path = tmp_path / "reports" / "gold.json"
    code = main(
        [
            "--dataset",
            str(dataset),
            "score-gold-batch",
            "--instance-ids",
            "test__repo-1",
            "--report-path",
            str(report_path),
        ]
    )
    assert code == EXIT_OK
    payload = json.loads(report_path.read_text())
    assert payload["total"] == 1
    assert payload["resolved"] == 1


def test_score_gold_batch_unknown_instance_exits_3(
    tmp_path: Path,
) -> None:
    dataset = _write_dataset_with_gold(
        tmp_path,
        instance_id="test__repo-1",
        gold_patch="diff\n",
    )
    code = main(
        [
            "--dataset",
            str(dataset),
            "score-gold-batch",
            "--instance-ids",
            "nope",
        ]
    )
    assert code == EXIT_NOT_FOUND
