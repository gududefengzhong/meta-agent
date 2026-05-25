"""Local workspace lifecycle for a SWE-bench instance (PR 2).

For PR 2 we run git locally via subprocess (no Docker, no
meta-agent yet). The harness clones the instance's GitHub
repository into a caller-chosen directory, checks out the
``base_commit``, and hands the path back. PR 3 adds the
container variant + test execution wrapper that runs against the
prebuilt SWE-bench eval images.

Network access
==============
The clone calls ``git clone`` against ``https://github.com/{repo}``
by default. Operators behind a proxy can override the base URL
via :func:`prepare_workspace`'s ``remote_url`` argument so they
can point at an in-cluster mirror or a file:// path (useful for
hermetic CI runs against a checked-out cache).

Error mapping
=============
Every subprocess error is wrapped in :class:`WorkspaceError` with
the failing command + stderr so callers can surface a structured
failure without exec'ing git themselves. Non-zero exit codes
from git are the most common; we also catch the rare
:class:`FileNotFoundError` when git is missing on PATH.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from eval.swebench.instances import SWEBenchInstance

logger = logging.getLogger(__name__)


_DEFAULT_GITHUB_BASE = "https://github.com/"


class WorkspaceError(Exception):
    """Raised when workspace preparation fails (git clone / checkout error)."""


def prepare_workspace(
    instance: SWEBenchInstance,
    dest: Path | str,
    *,
    remote_url: str | None = None,
    overwrite: bool = False,
) -> Path:
    """Clone ``instance.repo`` into ``dest`` and check out ``base_commit``.

    Returns the absolute workspace path on success.

    Args:
        instance: SWE-bench row whose ``repo`` + ``base_commit`` drive the clone.
        dest: Target directory. Created if absent. Must NOT already
            exist with content unless ``overwrite=True``.
        remote_url: Override the GitHub URL. Default:
            ``https://github.com/{instance.repo}``. Use a local
            mirror or a file:// path for hermetic CI.
        overwrite: If true, blow away ``dest`` first.
    """

    dest_path = Path(dest).resolve()
    if dest_path.exists():
        if not overwrite:
            raise WorkspaceError(
                f"workspace destination already exists: {dest_path} "
                "(pass overwrite=True to replace)"
            )
        shutil.rmtree(dest_path)
    resolved_url = remote_url if remote_url is not None else _default_remote(instance)

    _run_git(
        ["git", "clone", "--quiet", resolved_url, str(dest_path)],
        cwd=None,
        what=f"clone {instance.repo}",
    )
    _run_git(
        ["git", "checkout", "--quiet", instance.base_commit],
        cwd=dest_path,
        what=f"checkout {instance.base_commit}",
    )
    return dest_path


def _default_remote(instance: SWEBenchInstance) -> str:
    return f"{_DEFAULT_GITHUB_BASE}{instance.repo}.git"


def _run_git(
    cmd: Sequence[str],
    *,
    cwd: Path | None,
    what: str,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` with a tight error contract.

    Any non-zero exit, missing-executable, or unicode failure
    surfaces as :class:`WorkspaceError`. ``input_text`` feeds stdin
    when supplied (used by ``git apply --`` for the test_patch
    flow in :mod:`eval.swebench.patches`).
    """

    try:
        completed = subprocess.run(
            list(cmd),
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            input=input_text,
            check=False,
        )
    except FileNotFoundError as exc:
        raise WorkspaceError(f"{what}: git executable not found on PATH") from exc
    except UnicodeDecodeError as exc:  # pragma: no cover - rare
        raise WorkspaceError(f"{what}: non-utf8 git output: {exc}") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "(no stderr)"
        raise WorkspaceError(f"{what} failed (exit {completed.returncode}): {stderr}")
    return completed


__all__ = ["WorkspaceError", "prepare_workspace"]
