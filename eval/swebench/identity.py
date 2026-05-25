"""Identity helpers for eval reports (EVAL_BASELINE.md Standards 1 + 2).

Computes short, stable hashes for the two things a report needs to
pin its identity to:

* ``dataset_snapshot(path)`` — SHA-256[:12] of the dataset JSON
  file used by this run. Same dataset → same hash; safe for
  ``diff`` comparison between runs.
* ``harness_version()`` — short git SHA of the ``eval/swebench/``
  source tree at runtime. Falls back to ``"unknown"`` outside a
  git checkout so the CLI keeps working in stripped install
  layouts.

Kept separate from :mod:`eval.swebench.results` so the data
model has zero side-effects (no filesystem read, no subprocess)
and tests can stub the helpers when they want a deterministic
fixture identity.
"""

from __future__ import annotations

import hashlib
import subprocess
from functools import lru_cache
from pathlib import Path

_HASH_PREFIX = 12

_PACKAGE_DIR = Path(__file__).resolve().parent


def dataset_snapshot(path: Path) -> str:
    """Return SHA-256[:12] of ``path``.

    Use as the ``dataset_snapshot`` field on :class:`InstanceResult`
    so two reports run against the same fixture file share the
    same id, and any drift surfaces as a different hash.

    Raises:
        FileNotFoundError: ``path`` doesn't exist. The CLI passes
            the dataset path it actually loaded from, so a missing
            file is a programmer error worth surfacing.
    """

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest[:_HASH_PREFIX]


@lru_cache(maxsize=1)
def harness_version() -> str:
    """Return a short identifier for the harness code in this checkout.

    Best-effort: tries ``git rev-parse --short=12 HEAD`` rooted at
    the package directory. Returns ``"unknown"`` if git isn't
    available or the package isn't inside a git tree (eg installed
    from a wheel).

    Cached because the value is constant for the process lifetime
    and subprocess overhead would add up across a batch run.
    """

    try:
        result = subprocess.run(
            ["git", "rev-parse", f"--short={_HASH_PREFIX}", "HEAD"],
            cwd=_PACKAGE_DIR,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return "unknown"
    sha = result.stdout.strip()
    return sha or "unknown"


__all__ = ["dataset_snapshot", "harness_version"]
