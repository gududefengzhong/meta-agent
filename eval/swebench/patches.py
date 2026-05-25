"""Patch operations for a prepared SWE-bench workspace (PR 2).

Two public functions:

* :func:`apply_test_patch` — applies the instance's ``test_patch``
  to a workspace BEFORE the agent runs. The test_patch is what
  surfaces the FAIL_TO_PASS tests; without it the eval would have
  no acceptance signal. We deliberately keep this separate from
  the gold ``patch`` field, which the agent must never see.
* :func:`extract_patch` — emits the diff between
  ``instance.base_commit`` and the workspace's current tree. This
  is the agent's contribution; the eval harness later applies it
  to the test image to check whether FAIL_TO_PASS now passes.

Both wrap :func:`eval.swebench.workspace._run_git` so the error
contract (:class:`WorkspaceError`) is uniform with the rest of the
workspace layer — callers handle one exception type for any
subprocess failure.

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


def apply_test_patch(workspace: Path | str, patch_text: str) -> None:
    """Apply ``patch_text`` to ``workspace`` via ``git apply -``.

    Empty / whitespace-only patches are a no-op (some instances
    legitimately have an empty test_patch column). Anything that
    contains content but isn't a valid patch raises
    :class:`WorkspaceError` carrying git's stderr verbatim — the
    caller surfaces the failure to operators / CI.
    """

    ws = Path(workspace).resolve()
    if not ws.is_dir():
        raise WorkspaceError(f"workspace is not a directory: {ws}")
    if not patch_text.strip():
        return
    _run_git(
        ["git", "apply", "-"],
        cwd=ws,
        what="apply test_patch",
        input_text=patch_text,
    )


def extract_patch(workspace: Path | str, base_commit: str) -> str:
    """Return the diff between ``base_commit`` and the workspace's working tree.

    The diff includes both staged and unstaged changes (we pass
    ``--no-index``-free ``git diff`` so commits made by the agent
    in-workspace are captured). When the workspace tree is
    identical to ``base_commit`` the result is the empty string —
    expected when the agent finished without producing changes
    (likely a failure mode worth surfacing, but not an error in
    the extractor itself).
    """

    ws = Path(workspace).resolve()
    if not ws.is_dir():
        raise WorkspaceError(f"workspace is not a directory: {ws}")
    if not base_commit:
        raise WorkspaceError("base_commit must be a non-empty SHA")
    completed = _run_git(
        ["git", "diff", "--no-color", base_commit],
        cwd=ws,
        what=f"diff vs {base_commit}",
    )
    return completed.stdout


__all__ = ["apply_test_patch", "extract_patch"]
