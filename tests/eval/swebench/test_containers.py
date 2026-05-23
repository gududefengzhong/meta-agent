"""Unit tests for :mod:`eval.swebench.containers`.

We don't talk to the real Docker daemon. Tests inject a scripted
``_docker_run`` replacement that records every invocation and
returns canned ``CompletedProcess`` instances. This exercises the
full code path including the error mapping.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest
from eval.swebench.containers import Container, DockerError, ensure_image_pulled

from eval.swebench import containers


@dataclass
class _ScriptedDocker:
    """Records each (cmd, input_text) and emits scripted responses.

    ``responses`` is consumed FIFO by command-prefix match — the
    first entry whose first arg matches the request's first arg is
    used. ``default_returncode`` is the fallback when no prefix
    matches (useful for ``docker rm`` cleanups that happen on
    teardown).
    """

    responses: list[tuple[str, subprocess.CompletedProcess[str]]] = field(default_factory=list)
    default_returncode: int = 0
    calls: list[tuple[tuple[str, ...], str | None]] = field(default_factory=list)

    def run(
        self,
        cmd: Sequence[str],
        *,
        what: str,
        input_text: str | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        cmd_tuple = tuple(cmd)
        self.calls.append((cmd_tuple, input_text))
        verb = cmd_tuple[1] if len(cmd_tuple) > 1 else ""
        for idx, (match_verb, response) in enumerate(self.responses):
            if verb == match_verb:
                del self.responses[idx]
                if check and response.returncode != 0:
                    raise DockerError(
                        f"{what} failed (exit {response.returncode}): {response.stderr}"
                    )
                return response
        result = subprocess.CompletedProcess(list(cmd_tuple), self.default_returncode, "", "")
        if check and result.returncode != 0:
            raise DockerError(f"{what} failed (exit {result.returncode}): {result.stderr}")
        return result


@pytest.fixture
def scripted_docker(monkeypatch: pytest.MonkeyPatch) -> _ScriptedDocker:
    fake = _ScriptedDocker()
    monkeypatch.setattr(containers, "_docker_run", fake.run)
    return fake


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_ensure_image_pulled_skips_when_already_local(
    scripted_docker: _ScriptedDocker,
) -> None:
    scripted_docker.responses.append(("image", _completed(returncode=0)))
    ensure_image_pulled("swebench/foo:latest")
    verbs = [c[0][1] for c in scripted_docker.calls]
    assert verbs == ["image"]  # only inspect; no pull


def test_ensure_image_pulled_falls_through_to_pull_on_miss(
    scripted_docker: _ScriptedDocker,
) -> None:
    scripted_docker.responses.extend(
        [
            ("image", _completed(returncode=1, stderr="No such image")),
            ("pull", _completed(returncode=0)),
        ]
    )
    ensure_image_pulled("swebench/foo:latest")
    verbs = [c[0][1] for c in scripted_docker.calls]
    assert verbs == ["image", "pull"]


def test_ensure_image_pulled_rejects_empty_name() -> None:
    with pytest.raises(DockerError, match="non-empty"):
        ensure_image_pulled("")


async def test_container_lifecycle_runs_start_exec_stop(
    scripted_docker: _ScriptedDocker,
) -> None:
    scripted_docker.responses.extend(
        [
            ("run", _completed(returncode=0)),
            ("exec", _completed(returncode=0, stdout="ok")),
            ("rm", _completed(returncode=0)),
        ]
    )
    async with Container("swebench/foo:latest") as c:
        result = c.exec(["echo", "hi"])
    assert result.returncode == 0
    verbs = [call[0][1] for call in scripted_docker.calls]
    assert verbs == ["run", "exec", "rm"]


async def test_container_exec_with_stdin_passes_input(
    scripted_docker: _ScriptedDocker,
) -> None:
    scripted_docker.responses.extend(
        [
            ("run", _completed(returncode=0)),
            ("exec", _completed(returncode=0)),
            ("rm", _completed(returncode=0)),
        ]
    )
    async with Container("swebench/foo:latest") as c:
        c.exec(["git", "apply", "-"], input_text="diff --git ...")
    exec_call = next(call for call in scripted_docker.calls if call[0][1] == "exec")
    assert exec_call[1] == "diff --git ..."
    # ``docker exec -i`` is required when stdin is fed
    assert "-i" in exec_call[0]


async def test_container_exec_check_false_returns_failed_completed_process(
    scripted_docker: _ScriptedDocker,
) -> None:
    scripted_docker.responses.extend(
        [
            ("run", _completed(returncode=0)),
            ("exec", _completed(returncode=1, stdout="boom")),
            ("rm", _completed(returncode=0)),
        ]
    )
    async with Container("swebench/foo:latest") as c:
        result = c.exec(["pytest", "-x"], check=False)
    assert result.returncode == 1
    assert result.stdout == "boom"


async def test_container_stop_idempotent_after_implicit_teardown(
    scripted_docker: _ScriptedDocker,
) -> None:
    scripted_docker.responses.extend(
        [
            ("run", _completed(returncode=0)),
            ("rm", _completed(returncode=0)),
        ]
    )
    c = Container("swebench/foo:latest")
    async with c:
        pass
    # Second stop is a no-op — no extra `docker rm` issued.
    c.stop()
    verbs = [call[0][1] for call in scripted_docker.calls]
    assert verbs.count("rm") == 1


async def test_container_exec_before_start_raises() -> None:
    c = Container("swebench/foo:latest")
    with pytest.raises(DockerError, match="not started"):
        c.exec(["echo", "hi"])


def test_container_rejects_empty_image() -> None:
    with pytest.raises(DockerError, match="non-empty"):
        Container("")
