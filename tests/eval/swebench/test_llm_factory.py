"""Unit tests for :mod:`eval.swebench.llm_factory`."""

from __future__ import annotations

from pathlib import Path

import pytest
from eval.swebench.llm_factory import EvalLLMConfigError, build_default_llm

from meta_agent.infra.llm.openrouter import OpenRouterClient
from meta_agent.infra.llm.redacting import RedactingLLMClient


def _no_dotenv(tmp_path: Path) -> Path:
    """A guaranteed-missing dotenv path so tests don't accidentally
    pick up a real ``<repo>/.env`` on the developer's machine."""

    return tmp_path / "absent.env"


def test_missing_api_key_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(EvalLLMConfigError, match="API key not provided"):
        build_default_llm(env={}, dotenv_path=_no_dotenv(tmp_path))


def test_api_key_from_env_picked_up(tmp_path: Path) -> None:
    client = build_default_llm(
        env={"OPENROUTER_API_KEY": "tok-test"},
        dotenv_path=_no_dotenv(tmp_path),
    )
    # Outermost layer is redaction when ``redact=True`` (default).
    assert isinstance(client, RedactingLLMClient)


def test_explicit_api_key_overrides_env(tmp_path: Path) -> None:
    client = build_default_llm(
        api_key="explicit-key",
        env={"OPENROUTER_API_KEY": "from-env"},
        dotenv_path=_no_dotenv(tmp_path),
    )
    assert isinstance(client, RedactingLLMClient)


def test_redact_false_yields_bare_openrouter_client(tmp_path: Path) -> None:
    client = build_default_llm(api_key="k", redact=False, dotenv_path=_no_dotenv(tmp_path))
    assert isinstance(client, OpenRouterClient)
    assert not isinstance(client, RedactingLLMClient)


def test_blank_api_key_treated_as_missing(tmp_path: Path) -> None:
    with pytest.raises(EvalLLMConfigError):
        build_default_llm(
            env={"OPENROUTER_API_KEY": "   "},
            dotenv_path=_no_dotenv(tmp_path),
        )


# --------------------------------------------------------------- dotenv path


def test_api_key_from_dotenv_when_env_empty(tmp_path: Path) -> None:
    """``.env`` provides the key when the process env doesn't carry one."""

    dotenv = tmp_path / ".env"
    dotenv.write_text("OPENROUTER_API_KEY=tok-from-dotenv\n", encoding="utf-8")
    client = build_default_llm(env={}, dotenv_path=dotenv)
    assert isinstance(client, RedactingLLMClient)


def test_env_var_beats_dotenv(tmp_path: Path) -> None:
    """A live env var overrides the ``.env`` so one-off overrides work
    without editing the file."""

    dotenv = tmp_path / ".env"
    dotenv.write_text("OPENROUTER_API_KEY=in-dotenv\n", encoding="utf-8")
    # Process env wins. If the resolution order were wrong, the
    # explicit-api_key test below would still pass but a real run
    # would silently use the wrong key — so this test is the guard.
    client = build_default_llm(
        env={"OPENROUTER_API_KEY": "in-env"},
        dotenv_path=dotenv,
    )
    assert isinstance(client, RedactingLLMClient)


def test_explicit_api_key_beats_dotenv(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("OPENROUTER_API_KEY=in-dotenv\n", encoding="utf-8")
    client = build_default_llm(api_key="explicit", env={}, dotenv_path=dotenv)
    assert isinstance(client, RedactingLLMClient)


def test_dotenv_handles_quoted_values_and_export_prefix(tmp_path: Path) -> None:
    """Common ``.env`` shapes (double-quoted, single-quoted, ``export``
    prefix) all parse correctly. Reject ``#`` comments + blank lines."""

    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n"
        "# a comment\n"
        '  OPENROUTER_API_KEY="tok-quoted"\n'
        "export OTHER_KEY=bare\n"
        "MULTI_LINE_LIKE='single-quoted'\n",
        encoding="utf-8",
    )
    client = build_default_llm(env={}, dotenv_path=dotenv)
    assert isinstance(client, RedactingLLMClient)


def test_dotenv_blank_value_falls_through_to_missing(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("OPENROUTER_API_KEY=\n", encoding="utf-8")
    with pytest.raises(EvalLLMConfigError):
        build_default_llm(env={}, dotenv_path=dotenv)


def test_dotenv_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """The dotenv lookup never raises on missing file — it just
    contributes nothing. Only the final ""no key anywhere"" check
    raises."""

    with pytest.raises(EvalLLMConfigError):
        build_default_llm(env={}, dotenv_path=tmp_path / "definitely-not-there.env")
