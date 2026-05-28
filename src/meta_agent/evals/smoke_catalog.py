"""Catalog helpers for the external ``meta-agent-smoke`` baseline repo."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

import httpx

from meta_agent.cli.client import EXIT_NETWORK, EXIT_USAGE, CLIError

DEFAULT_REMOTE_CATALOG_URL = (
    "https://raw.githubusercontent.com/gududefengzhong/meta-agent-smoke/master/catalog/cases.json"
)
DEFAULT_REPO_URL = "https://github.com/gududefengzhong/meta-agent-smoke.git"
DEFAULT_VERIFY_SUITE = "python_test"
DEFAULT_MODEL = "deepseek/deepseek-v4-pro"

SmokeCase = Mapping[str, object]


def default_catalog_source() -> str:
    return DEFAULT_REMOTE_CATALOG_URL


def default_repo_url() -> str:
    return DEFAULT_REPO_URL


def default_verify_suite() -> str:
    return DEFAULT_VERIFY_SUITE


def default_model() -> str:
    return DEFAULT_MODEL


def is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


async def load_cases(source: str) -> list[dict[str, object]]:
    if is_http_url(source):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
                response = await client.get(source)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise CLIError(
                EXIT_NETWORK, f"failed to fetch smoke catalog {source}: {exc!s}"
            ) from exc
        if response.status_code != 200:
            raise CLIError(
                EXIT_USAGE, f"failed to fetch smoke catalog {source}: HTTP {response.status_code}"
            )
        try:
            decoded = response.json()
        except ValueError as exc:
            raise CLIError(EXIT_USAGE, f"smoke catalog {source} is not valid JSON") from exc
        return validate_cases(decoded, source=source)
    path = Path(source)
    if not path.is_file():
        raise CLIError(EXIT_USAGE, f"smoke catalog not found: {source}")
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CLIError(EXIT_USAGE, f"smoke catalog {source} is not valid JSON") from exc
    return validate_cases(decoded, source=source)


def validate_cases(raw: object, *, source: str) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        raise CLIError(EXIT_USAGE, f"smoke catalog {source} must be a JSON array")
    cases: list[dict[str, object]] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise CLIError(EXIT_USAGE, f"smoke catalog {source} entry #{idx} must be a JSON object")
        name = item.get("case")
        issue_description = item.get("issue_description")
        target_files = item.get("target_files")
        if not isinstance(name, str) or not name:
            raise CLIError(EXIT_USAGE, f"smoke catalog {source} entry #{idx} missing case")
        if not isinstance(issue_description, str) or not issue_description:
            raise CLIError(
                EXIT_USAGE, f"smoke catalog {source} entry {name!r} missing issue_description"
            )
        if (
            not isinstance(target_files, list)
            or not target_files
            or not all(isinstance(value, str) and value for value in target_files)
        ):
            raise CLIError(
                EXIT_USAGE, f"smoke catalog {source} entry {name!r} missing target_files"
            )
        cases.append(item)
    return cases


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def select_cases(
    cases: list[SmokeCase],
    *,
    case_names: list[str],
    batches: list[str],
    categories: list[str],
) -> list[SmokeCase]:
    selected_names = set(case_names)
    selected_batches = {value.lower() for value in batches}
    selected_categories = {value.lower() for value in categories}
    filtered: list[SmokeCase] = []
    for case in cases:
        name = str(case.get("case", ""))
        batch = str(case.get("batch", "")).lower()
        case_categories = {value.lower() for value in _string_list(case.get("categories"))}
        if selected_names and name not in selected_names:
            continue
        if selected_batches and batch not in selected_batches:
            continue
        if selected_categories and not (case_categories & selected_categories):
            continue
        filtered.append(case)
    return filtered


def build_payload(
    case: SmokeCase,
    *,
    repo_url: str,
    verify_suite: str,
    model: str,
) -> dict[str, object]:
    return {
        "issue_description": case["issue_description"],
        "repo_url": repo_url,
        "base_ref": case["case"],
        "target_files": _string_list(case.get("target_files")),
        "verify_suite": verify_suite,
        "model": model,
    }


def render_case_summary(case: SmokeCase) -> str:
    categories = ", ".join(_string_list(case.get("categories")))
    return f"{case['case']} | batch={case.get('batch', '-')} | categories={categories}"
