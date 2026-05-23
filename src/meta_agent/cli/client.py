"""HTTP client + SSE helpers for the CLI.

A thin layer over ``httpx`` that knows the meta-agent task API
shape: submit a task, tail the lifecycle events stream, tail the
LLM token stream. Kept separate from the argparse dispatch so the
network surface can be unit-tested against an ASGI app + mock
transport.

Errors surface as :class:`CLIError` with a stable exit code:

* :data:`EXIT_USAGE` (2) — bad args / missing config / 4xx from server
* :data:`EXIT_NETWORK` (3) — connection refused / DNS / timeout
* :data:`EXIT_TASK_FAILED` (4) — task reached a non-SUCCEEDED terminal state
* :data:`EXIT_OK` (0) — task succeeded

The CLI's caller pattern is::

    cfg = CLIConfig.from_env(args)
    async with TaskClient(cfg) as client:
        task_id = await client.submit_task(...)
        await client.tail_task(task_id, on_chunk=..., on_event=...)

so every command can share the same client without re-deriving the
auth header / base URL.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import httpx

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NETWORK = 3
EXIT_TASK_FAILED = 4

_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "expired"})

_API_URL_ENV = "META_AGENT_API_URL"
_TOKEN_ENV = "META_AGENT_TOKEN"
_DEFAULT_API_URL = "http://localhost:8000"


class CLIError(Exception):
    """Carrier for a (exit_code, message) pair surfaced by the CLI."""

    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message


@dataclass(frozen=True)
class CLIConfig:
    """Resolved CLI configuration after env + flag merging."""

    api_url: str
    token: str

    @classmethod
    def from_env(
        cls,
        *,
        api_url: str | None = None,
        token: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CLIConfig:
        e = env if env is not None else os.environ
        resolved_url = api_url or e.get(_API_URL_ENV, _DEFAULT_API_URL)
        resolved_token = token or e.get(_TOKEN_ENV, "")
        if not resolved_token:
            raise CLIError(
                EXIT_USAGE,
                f"missing bearer token: set ${_TOKEN_ENV} or pass --token",
            )
        return cls(api_url=resolved_url.rstrip("/"), token=resolved_token)


class TaskClient:
    """Authenticated HTTP wrapper for the meta-agent task API."""

    def __init__(
        self,
        config: CLIConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.api_url,
            headers={"Authorization": f"Bearer {config.token}"},
            timeout=httpx.Timeout(30.0, read=None),  # no read timeout on SSE
            transport=transport,
        )

    async def __aenter__(self) -> TaskClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._client.aclose()

    async def submit_task(
        self,
        *,
        task_type: str,
        input_payload: dict[str, Any],
        idempotency_key: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """POST /v1/tasks. Returns the parsed task response dict.

        Raises :class:`CLIError` on non-2xx (EXIT_USAGE for 4xx,
        EXIT_NETWORK for transport failures).
        """
        body: dict[str, Any] = {
            "task_type": task_type,
            "input_payload": input_payload,
        }
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key
        if session_id is not None:
            body["session_id"] = session_id
        try:
            resp = await self._client.post("/v1/tasks", json=body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise CLIError(
                EXIT_NETWORK,
                f"network error reaching {self._config.api_url}: {exc!s}",
            ) from exc
        return _decode_or_raise(resp)

    async def get_task(self, task_id: str) -> dict[str, Any]:
        try:
            resp = await self._client.get(f"/v1/tasks/{task_id}")
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise CLIError(
                EXIT_NETWORK,
                f"network error reaching {self._config.api_url}: {exc!s}",
            ) from exc
        return _decode_or_raise(resp)

    async def stream_llm_chunks(self, task_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed chunks from /v1/tasks/{id}/llm-stream."""
        path = f"/v1/tasks/{task_id}/llm-stream"
        try:
            async with self._client.stream("GET", path) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise _http_error(resp, expected="200 SSE")
                async for chunk in _iter_sse_data(resp):
                    yield chunk
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise CLIError(
                EXIT_NETWORK,
                f"llm-stream transport error: {exc!s}",
            ) from exc

    async def stream_events(self, task_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield parsed lifecycle events from /v1/tasks/{id}/events."""
        path = f"/v1/tasks/{task_id}/events"
        try:
            async with self._client.stream("GET", path) as resp:
                if resp.status_code != 200:
                    await resp.aread()
                    raise _http_error(resp, expected="200 SSE")
                async for event in _iter_sse_data(resp):
                    yield event
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise CLIError(
                EXIT_NETWORK,
                f"events stream transport error: {exc!s}",
            ) from exc


# --------------------------------------------------------------- helpers


def _decode_or_raise(resp: httpx.Response) -> dict[str, Any]:
    if 200 <= resp.status_code < 300:
        try:
            body = resp.json()
        except ValueError as exc:
            raise CLIError(EXIT_NETWORK, f"server returned non-JSON body: {exc!s}") from exc
        if not isinstance(body, dict):
            raise CLIError(EXIT_NETWORK, "server response is not a JSON object")
        return body
    raise _http_error(resp)


def _http_error(resp: httpx.Response, *, expected: str | None = None) -> CLIError:
    detail = ""
    try:
        body = resp.json()
        if isinstance(body, dict) and "detail" in body:
            detail = f": {body['detail']}"
    except ValueError:
        text = resp.text
        if text:
            detail = f": {text[:200]}"
    suffix = f" (expected {expected})" if expected else ""
    exit_code = EXIT_USAGE if resp.status_code < 500 else EXIT_NETWORK
    return CLIError(
        exit_code,
        f"HTTP {resp.status_code}{suffix}{detail}",
    )


async def _iter_sse_data(resp: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Parse SSE ``data:`` payloads as JSON; skip framing-only lines.

    Each event arrives as ``data: {json}\n\n``. We deliberately
    ignore ``event:`` / ``id:`` / comment lines — the caller cares
    about the JSON body and reconstructs event type from the
    payload itself when needed.
    """
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue  # malformed event — skip rather than abort
        if isinstance(obj, dict):
            yield obj


def is_terminal_state(state: str | None) -> bool:
    return state is not None and state in _TERMINAL_STATES


ChunkHandler = Callable[[dict[str, Any]], None]
EventHandler = Callable[[dict[str, Any]], None]
