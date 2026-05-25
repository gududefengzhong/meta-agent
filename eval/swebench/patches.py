"""Patch operations for a prepared SWE-bench workspace.

Two public functions:

* :func:`apply_test_patch` — applies the instance's ``test_patch``
  to a workspace before the agent runs, **and commits the result
  as a new commit** so :func:`extract_patch` can later isolate the
  agent's net edits from the test_patch additions. Returns the
  new HEAD SHA. Even when ``test_patch`` is empty, the function
  returns the current HEAD SHA (the base_commit) so callers
  have a uniform "post-test_patch ref" to diff against.
* :func:`extract_patch` — emits the diff between the supplied
  ``base_ref`` and the workspace's current tree. Caller should
  pass the SHA returned by :func:`apply_test_patch` so the
  resulting patch contains the agent's edits only.

Why test_patch is committed
===========================
The eval container re-applies the dataset's ``test_patch``
independently. If the agent's extracted patch already contains
the test_patch additions (as it would when ``extract_patch`` is
called with the original ``base_commit``), ``git apply`` inside
the container conflicts with itself and the run fails to score.
Committing test_patch first means the post-application HEAD is
the right diff base — agent edits land *on top* and diff cleanly.

This is the same pattern upstream SWE-bench's reference harness
uses internally; we mirror it so our scoring agrees with theirs
on instances whose ``test_patch`` overlaps lines the agent might
touch (e.g. when the agent adds a test alongside its fix).

Patch-formatting nits
=====================
``git diff`` emits the trailing newline so consumers don't need
to add one. ``apply_test_patch`` feeds the patch via stdin
because writing it to a tempfile then re-reading is more code
for no benefit — git understands ``--`` + stdin natively.
"""

from __future__ import annotations

from pathlib import Path

from eval.swebench.workspace import WorkspaceError, _run_git

_HARNESS_COMMITTER_EMAIL = "harness@eval.swebench"
_HARNESS_COMMITTER_NAME = "swebench-eval-harness"


def apply_test_patch(workspace: Path | str, patch_text: str) -> str:
    """Apply ``patch_text`` to ``workspace``, commit it, and return the new HEAD SHA.

    Empty / whitespace-only patches are a no-op as far as the
    working tree is concerned — but the function still returns the
    current HEAD SHA so callers have a uniform "post-test_patch
    ref" to thread through :func:`extract_patch`.

    Anything that contains content but isn't a valid patch raises
    :class:`WorkspaceError` carrying git's stderr verbatim — the
    caller surfaces the failure to operators / CI.
    """

    ws = Path(workspace).resolve()
    if not ws.is_dir():
        raise WorkspaceError(f"workspace is not a directory: {ws}")
    if not patch_text.strip():
        return _head_sha(ws)
    _run_git(
        ["git", "apply", "-"],
        cwd=ws,
        what="apply test_patch",
        input_text=patch_text,
    )
    _run_git(
        ["git", "add", "-A"],
        cwd=ws,
        what="stage test_patch",
    )
    _run_git(
        [
            "git",
            "-c",
            f"user.email={_HARNESS_COMMITTER_EMAIL}",
            "-c",
            f"user.name={_HARNESS_COMMITTER_NAME}",
            "commit",
            "--quiet",
            "-m",
            "harness: apply test_patch",
        ],
        cwd=ws,
        what="commit test_patch",
    )
    return _head_sha(ws)


def extract_patch(workspace: Path | str, base_ref: str) -> str:
    """Return the diff between ``base_ref`` and the workspace's working tree.

    ``base_ref`` is the SHA returned by :func:`apply_test_patch` —
    pass that rather than ``instance.base_commit`` so the diff
    excludes the test_patch (which the eval container will apply
    independently and would otherwise conflict).

    The diff includes both staged and unstaged changes. When the
    workspace tree is identical to ``base_ref`` the result is the
    empty string — expected when the agent finished without
    producing changes.
    """

    ws = Path(workspace).resolve()
    if not ws.is_dir():
        raise WorkspaceError(f"workspace is not a directory: {ws}")
    if not base_ref:
        raise WorkspaceError("base_ref must be a non-empty SHA")
    completed = _run_git(
        ["git", "diff", "--no-color", base_ref],
        cwd=ws,
        what=f"diff vs {base_ref}",
    )
    return completed.stdout


def _head_sha(workspace: Path) -> str:
    completed = _run_git(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        what="rev-parse HEAD",
    )
    return completed.stdout.strip()


__all__ = ["apply_test_patch", "extract_patch"]
