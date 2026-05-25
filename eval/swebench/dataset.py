"""Load SWE-bench instances from a local JSON file.

Why JSON-file loading instead of pulling from HuggingFace
=========================================================
For PR 1 (scaffold) we want the harness to work offline with no
``datasets`` / ``huggingface_hub`` dependency. The next PR will
add a thin downloader that produces the same JSON layout this
loader already understands, so the on-disk format is the stable
boundary between "where instances come from" and "what the
harness consumes".

JSON shape
==========
Each file is a JSON array. Every element is a dict with the
SWE-bench column names (snake_case). Unrecognised keys land in
:attr:`SWEBenchInstance.extra`. The loader normalises the
dataset's list-valued ``FAIL_TO_PASS`` / ``PASS_TO_PASS`` columns
into tuples and renames them to lowercase to match the pydantic
field names.

A small fixture lives next to this module
(``fixtures/instances_sample.json``) — used by unit tests and by
the CLI's offline ``list`` command so the harness has *something*
to show before the real dataset is pulled.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from eval.swebench.instances import SWEBenchInstance

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "instances_sample.json"


def builtin_dataset_path() -> Path:
    """Return the path of the built-in fixture dataset.

    Stable across the process lifetime. Used by callers that need
    the dataset *path* (not its contents) — eg the identity layer
    hashing it for ``dataset_snapshot``.
    """

    return _FIXTURE_PATH


def resolve_dataset_path(path: Path | str | None) -> Path:
    """Return the effective dataset path: ``path`` if given, else the fixture."""

    return Path(path) if path is not None else _FIXTURE_PATH


class SWEBenchDatasetError(Exception):
    """Raised when a dataset file is missing, malformed, or schema-invalid."""


def load_instances(
    path: Path | str | None = None,
    *,
    repos: Iterable[str] | None = None,
    limit: int | None = None,
) -> list[SWEBenchInstance]:
    """Read instances from ``path`` (default: the checked-in fixture).

    Optional filters:

    * ``repos`` — keep only instances whose ``repo`` is in this set.
    * ``limit`` — return at most this many instances after filtering.

    Filtering happens after parse + validate so a malformed row
    surfaces even if it would have been filtered away.
    """

    resolved = Path(path) if path is not None else _FIXTURE_PATH
    if not resolved.is_file():
        raise SWEBenchDatasetError(f"dataset file not found: {resolved}")
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SWEBenchDatasetError(f"{resolved}: invalid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise SWEBenchDatasetError(f"{resolved}: top-level must be a JSON array of instances")

    instances: list[SWEBenchInstance] = []
    for idx, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise SWEBenchDatasetError(f"{resolved}: row {idx} is not a JSON object")
        try:
            instances.append(_parse_row(entry))
        except ValidationError as exc:
            raise SWEBenchDatasetError(f"{resolved}: row {idx} failed validation: {exc}") from exc

    if repos is not None:
        repo_set = frozenset(repos)
        instances = [inst for inst in instances if inst.repo in repo_set]
    if limit is not None:
        if limit < 0:
            raise SWEBenchDatasetError(f"limit must be >= 0, got {limit}")
        instances = instances[:limit]
    return instances


def load_instance(instance_id: str, path: Path | str | None = None) -> SWEBenchInstance:
    """Load exactly one instance by id; raises if absent."""

    for inst in load_instances(path):
        if inst.instance_id == instance_id:
            return inst
    raise SWEBenchDatasetError(f"instance_id {instance_id!r} not found in dataset")


# ---------------------------------------------------- internal


_LIST_COLUMNS = ("FAIL_TO_PASS", "PASS_TO_PASS")
_KNOWN_COLUMN_MAP = {
    "instance_id": "instance_id",
    "repo": "repo",
    "base_commit": "base_commit",
    "problem_statement": "problem_statement",
    "patch": "patch",
    "test_patch": "test_patch",
    "FAIL_TO_PASS": "fail_to_pass",
    "PASS_TO_PASS": "pass_to_pass",
    "version": "version",
    "environment_setup_commit": "environment_setup_commit",
}


def _parse_row(row: dict[str, Any]) -> SWEBenchInstance:
    """Map a SWE-bench-shaped dict into :class:`SWEBenchInstance`.

    Renames the upstream UPPER_SNAKE list columns to lowercase
    matching the pydantic field names; everything not in the known
    column set lands in ``extra`` verbatim so the loader is
    forward-compatible with future dataset columns.
    """

    fields: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    for key, value in row.items():
        if key in _LIST_COLUMNS:
            fields[_KNOWN_COLUMN_MAP[key]] = _normalise_test_selectors(value)
            continue
        if key in _KNOWN_COLUMN_MAP:
            fields[_KNOWN_COLUMN_MAP[key]] = value
            continue
        extra[key] = value
    if extra:
        fields["extra"] = extra
    return SWEBenchInstance.model_validate(fields)


def _normalise_test_selectors(value: Any) -> tuple[str, ...]:
    """Accept either a JSON array or a JSON-encoded string (HF quirk)."""

    if isinstance(value, list):
        return tuple(str(v) for v in value)
    if isinstance(value, str):
        # The HuggingFace dataset sometimes encodes the list as a
        # JSON string column — accept both shapes.
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise SWEBenchDatasetError(f"test selectors are not valid JSON: {value!r}") from exc
        if not isinstance(parsed, list):
            raise SWEBenchDatasetError(
                f"test selectors must decode to a list, got {type(parsed).__name__}"
            )
        return tuple(str(v) for v in parsed)
    raise SWEBenchDatasetError(
        f"test selectors must be a list or JSON string, got {type(value).__name__}"
    )


__all__ = [
    "SWEBenchDatasetError",
    "builtin_dataset_path",
    "load_instance",
    "load_instances",
    "resolve_dataset_path",
]
