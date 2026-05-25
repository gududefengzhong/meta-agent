"""Docker container lifecycle for SWE-bench evaluation (PR 3).

Two responsibilities:

1. :func:`ensure_image_pulled` — verify (and pull on demand) one of
   the prebuilt ``swebench/sweb.eval.*`` images. Pulls are slow
   (multi-GB per image) and bandwidth-expensive, so the helper
   checks ``docker inspect`` first and only pulls on miss.
2. :class:`Container` — async context manager that ``docker run``s
   the image with ``sleep infinity`` as entrypoint, exposes
   :meth:`exec` for running commands inside, and tears down on
   exit. The container is ephemeral — every evaluation gets a
   fresh one so a failed run can't poison the next.

We shell out to the ``docker`` CLI instead of taking a runtime
dependency on the ``docker`` PyPI client. The CLI is universally
available wherever a SWE-bench eval runs, and avoiding the
heavyweight client keeps ``eval/`` standalone.

Error mapping
=============
Every subprocess failure raises :class:`DockerError` carrying the
exit code + stderr. Callers handle a single exception type for
any container-side surface.
"""

from __future__ import annotations

import logging
import subprocess
import uuid
from collections.abc import Sequence
from typing import Self

logger = logging.getLogger(__name__)


class DockerError(Exception):
    """Raised when a docker CLI invocation fails (non-zero exit, missing binary)."""


def ensure_image_pulled(image: str) -> None:
    """Pull ``image`` if it isn't already cached locally.

    Cheap when the image is present (one ``docker inspect`` call);
    expensive on miss (multi-GB pull). The first call for a given
    image is the slow one — subsequent runs in the same env are
    fast.
    """

    if not image:
        raise DockerError("image name must be non-empty")
    inspect = _docker_run(
        ["docker", "image", "inspect", image],
        what=f"inspect {image}",
        check=False,
    )
    if inspect.returncode == 0:
        return
    _docker_run(["docker", "pull", image], what=f"pull {image}")


class Container:
    """Async context manager wrapping ``docker run`` for evaluation.

    Usage::

        async with Container(image="swebench/sweb.eval.x86_64.foo:latest") as c:
            result = c.exec(["pytest", "tests/test_x.py::test_a"])
            assert result.returncode == 0

    The container runs ``sleep infinity`` so a caller can issue
    multiple ``exec`` calls (apply patch, run tests, capture
    output) before tearing it down. ``--rm`` ensures the container
    is removed even if ``stop`` is somehow missed.

    Workdir defaults to ``/testbed`` because that's where the
    SWE-bench eval images check out their repo; override
    ``workdir=`` for non-stock images.
    """

    def __init__(
        self,
        image: str,
        *,
        workdir: str = "/testbed",
        name: str | None = None,
    ) -> None:
        if not image:
            raise DockerError("image must be non-empty")
        self._image = image
        self._workdir = workdir
        self._name = name or f"swebench-eval-{uuid.uuid4().hex[:12]}"
        self._started = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def image(self) -> str:
        return self._image

    async def __aenter__(self) -> Self:
        self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.stop()

    def start(self) -> None:
        """``docker run -d`` the image with ``sleep infinity`` as entrypoint."""

        if self._started:
            raise DockerError(f"container {self._name!r} already started")
        _docker_run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                self._name,
                "--workdir",
                self._workdir,
                "--entrypoint",
                "sleep",
                self._image,
                "infinity",
            ],
            what=f"run {self._image}",
        )
        self._started = True

    def stop(self) -> None:
        """``docker rm -f`` the container; idempotent."""

        if not self._started:
            return
        # Use rm -f instead of stop+rm to skip the graceful-shutdown
        # wait; sleep-infinity containers have nothing to flush.
        _docker_run(
            ["docker", "rm", "-f", self._name],
            what=f"rm {self._name}",
            check=False,
        )
        self._started = False

    def exec(
        self,
        cmd: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``cmd`` inside the container; return the full :class:`CompletedProcess`.

        ``input_text`` is fed via stdin (used by ``git apply -``).
        ``check=False`` returns the failed CompletedProcess instead
        of raising — pytest is expected to exit non-zero on test
        failures and the caller parses the output regardless.
        """

        if not self._started:
            raise DockerError(f"container {self._name!r} not started")
        docker_cmd: list[str] = ["docker", "exec"]
        if input_text is not None:
            docker_cmd.append("-i")
        docker_cmd.append(self._name)
        docker_cmd.extend(cmd)
        return _docker_run(
            docker_cmd,
            what=f"exec in {self._name}: {cmd[0] if cmd else '<empty>'}",
            input_text=input_text,
            check=check,
        )


def _docker_run(
    cmd: Sequence[str],
    *,
    what: str,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Single subprocess seam for the container layer.

    Tests monkeypatch this function module-level to inject scripted
    docker responses without touching the real Docker daemon.
    """

    try:
        completed = subprocess.run(
            list(cmd),
            capture_output=True,
            text=True,
            input=input_text,
            check=False,
        )
    except FileNotFoundError as exc:
        raise DockerError(f"{what}: docker executable not found on PATH") from exc
    if check and completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip() or "(no stderr)"
        raise DockerError(f"{what} failed (exit {completed.returncode}): {stderr}")
    return completed


__all__ = ["Container", "DockerError", "ensure_image_pulled"]
