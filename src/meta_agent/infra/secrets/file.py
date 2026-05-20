"""File-backed :class:`Secrets` adapter.

Reads a JSON file at construction time and serves lookups from the
parsed dict. Intended for Kubernetes deployments that mount a secret
as a volume rather than as environment variables, and for local dev
sandboxes that prefer ``./secrets.json`` over a fragile ``.env`` file.

The file is opened once and the parsed mapping is cached for the life
of the adapter — secrets do not rotate without a process restart
today. KMS / Vault remain port-stub only.
"""

from __future__ import annotations

import json
from pathlib import Path

from meta_agent.core.ports.secrets import SecretBackendError, SecretNotFoundError, Secrets


class FileSecrets(Secrets):
    """Load secrets from a JSON file at construction time.

    The file must parse to a flat ``{key: value}`` object where each
    value is a string. Nested objects, arrays, and non-string scalars
    raise :class:`SecretBackendError` so we fail loudly on
    malformed config rather than silently returning ``""``.
    """

    def __init__(self, values: dict[str, str]) -> None:
        # Defensive copy so callers cannot mutate the resolved map
        # after construction.
        self._values = dict(values)

    @classmethod
    def from_path(cls, path: Path | str) -> FileSecrets:
        """Open ``path`` as JSON and validate the schema."""
        file_path = Path(path)
        try:
            raw = file_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SecretBackendError(f"unable to read secrets file {file_path}: {exc}") from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SecretBackendError(f"secrets file {file_path} is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SecretBackendError(
                f"secrets file {file_path} must contain a JSON object, got {type(parsed).__name__}"
            )
        values: dict[str, str] = {}
        for key, value in parsed.items():
            if not isinstance(key, str):
                raise SecretBackendError(f"secrets file {file_path} has non-string key {key!r}")
            if not isinstance(value, str):
                raise SecretBackendError(
                    f"secrets file {file_path} key {key!r} maps to non-string value"
                )
            values[key] = value
        return cls(values)

    async def get(self, key: str) -> str:
        if key not in self._values:
            raise SecretNotFoundError(f"secret key {key!r} not present in file")
        value = self._values[key].strip()
        if not value:
            raise SecretNotFoundError(f"secret key {key!r} is present but empty")
        return value


__all__ = ["FileSecrets"]
