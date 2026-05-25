"""Minimal LLM client factory for eval runs.

The worker bootstrap wires a full LLM stack (broadcasting + redaction
+ routing + budget + rate-limit + metering + circuit-breaker +
OpenRouter). For benchmark runs we want fewer layers — the eval is
running against ourselves, doesn't need per-call audit rows, and
should not be subject to per-tenant rate limits that exist for
production traffic.

This factory builds the slimmest viable stack:

* :class:`OpenRouterClient` — the actual provider
* :class:`RedactingLLMClient` (optional) — keeps eval logs free of
  secrets in the rare case a SWE-bench fixture leaks one
* :class:`RoutingLLMClient` (optional) — when the operator wants
  per-step-kind model selection during eval (cost control on
  large runs)

All else is deliberately skipped. Production-time wrappers
(circuit-breaker / rate-limit / budget) would just complicate the
eval signal without adding value.

Configuration
=============
The factory resolves the OpenRouter API key in this order:

1. Explicit ``api_key`` kwarg (CLI ``--api-key``)
2. ``OPENROUTER_API_KEY`` in the process environment
3. ``OPENROUTER_API_KEY`` in ``<repo-root>/.env`` (gitignored,
   convention for local dev secrets)

Process env beats the ``.env`` file so a one-off override works
without editing the file. The ``.env`` parser is tiny (10 lines,
no new dependency) — full ``python-dotenv`` semantics like
variable expansion or multiline values are out of scope; if a
``.env`` ever needs those, switch to the real library.
"""

from __future__ import annotations

import os
from pathlib import Path

from meta_agent.core.ports.llm import LLMClient
from meta_agent.infra.llm.config import OpenRouterConfig
from meta_agent.infra.llm.openrouter import OpenRouterClient
from meta_agent.infra.llm.redacting import RedactingLLMClient
from meta_agent.infra.redaction import Redactor

_DEFAULT_MODEL = "deepseek/deepseek-chat"

# Repo root = ``eval/swebench/llm_factory.py``.parents[2]
_REPO_ROOT = Path(__file__).resolve().parents[2]


class EvalLLMConfigError(Exception):
    """Raised when the LLM factory cannot construct a client (e.g. missing key)."""


def build_default_llm(
    *,
    api_key: str | None = None,
    default_model: str = _DEFAULT_MODEL,
    base_url: str | None = None,
    redact: bool = True,
    env: dict[str, str] | None = None,
    dotenv_path: Path | None = None,
) -> LLMClient:
    """Build a slim eval-time :class:`LLMClient`.

    Key resolution order (first non-blank wins):
    1. ``api_key`` kwarg
    2. ``OPENROUTER_API_KEY`` in ``env`` (or ``os.environ`` when
       ``env is None``)
    3. ``OPENROUTER_API_KEY`` in ``dotenv_path`` (or
       ``<repo-root>/.env`` when ``dotenv_path is None``)

    Missing key raises :class:`EvalLLMConfigError` with a clear
    message. ``redact=True`` wraps the OpenRouter client in
    :class:`RedactingLLMClient` so any echoed secrets land
    scrubbed in the eval logs.
    """

    e = env if env is not None else dict(os.environ)
    explicit = api_key.strip() if api_key else ""
    env_key = e.get("OPENROUTER_API_KEY", "").strip()
    dotenv_key = ""
    if not explicit and not env_key:
        path = dotenv_path if dotenv_path is not None else _REPO_ROOT / ".env"
        loaded = _load_dotenv(path)
        dotenv_key = loaded.get("OPENROUTER_API_KEY", "").strip()
    resolved_key = explicit or env_key or dotenv_key
    if not resolved_key:
        raise EvalLLMConfigError(
            "OpenRouter API key not provided: set OPENROUTER_API_KEY in your env, "
            "add it to <repo-root>/.env, or pass api_key="
        )
    config_kwargs: dict[str, str] = {
        "api_key": resolved_key,
        "default_model": default_model,
    }
    if base_url is not None:
        config_kwargs["base_url"] = base_url
    config = OpenRouterConfig(**config_kwargs)  # type: ignore[arg-type]
    client: LLMClient = OpenRouterClient(config)
    if redact:
        client = RedactingLLMClient(client, redactor=Redactor())
    return client


def _load_dotenv(path: Path) -> dict[str, str]:
    """Parse a ``KEY=value`` ``.env`` file into a dict.

    Intentionally minimal: ignores blank lines and ``#`` comments,
    strips surrounding single/double quotes from values, splits on
    the first ``=``. Anything fancier (variable expansion, multiline
    values, export prefix handling) is out of scope — if a use
    case needs it, take a real ``python-dotenv`` dep at that point.

    Returns an empty dict when ``path`` doesn't exist or isn't a
    file; never raises, since a missing ``.env`` is the common case
    in CI and shouldn't be a hard failure.
    """

    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        # Strip a matching surrounding pair of single or double quotes.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        out[key] = value
    return out


__all__ = ["EvalLLMConfigError", "build_default_llm"]
