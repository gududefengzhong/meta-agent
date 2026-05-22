"""httpx-backed :class:`WebFetchTool` with a domain allow-list.

Phase β+ scope:

* GET-only; the agent loop has no use case for POST yet and refusing
  it keeps the audit story (URL + status code is the whole story)
  simple.
* HTTP and HTTPS only — file:// / data:// / ssh:// schemes are
  refused outright with :class:`ToolPermissionError`.
* Hostname is matched against an explicit allow-list (substring on
  the hostname suffix, with a leading dot so ``docs.example.com``
  matches an entry of ``example.com`` but ``evilexample.com`` does
  not).
* Response body is bounded by ``ctx.output_byte_cap``; anything past
  the cap is dropped with ``truncated=true``. Binary content types
  are refused so the LLM never sees base64'd bytes.

Errors:

* URL parsing failure / scheme not http(s) → :class:`ToolValidationError`
* Hostname not in allow-list → :class:`ToolPermissionError`
* Network error / timeout → :class:`ToolExecutionError`
  (category EXTERNAL)
* Non-2xx HTTP responses are *not* errors — they return populated
  :class:`WebFetchOutcome` so the agent loop can branch on
  ``status``.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import httpx

from meta_agent.core.ports.tools import (
    ToolContext,
    ToolExecutionError,
    ToolPermissionError,
    ToolValidationError,
    WebFetchOutcome,
    WebFetchTool,
)

_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_MAX_REDIRECTS = 3
# Permitted Content-Type prefixes. The LLM consumes UTF-8 text only.
_TEXT_CONTENT_TYPE_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/xhtml+xml",
    "application/javascript",
)


class HttpxWebFetchTool(WebFetchTool):
    """Single-process :class:`WebFetchTool` backed by an httpx async client."""

    def __init__(
        self,
        *,
        allowed_hosts: frozenset[str],
        default_timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_redirects: int = _DEFAULT_MAX_REDIRECTS,
        client_factory: object = None,
    ) -> None:
        if not allowed_hosts:
            raise ValueError("allowed_hosts must contain at least one hostname")
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive")
        if max_redirects < 0:
            raise ValueError("max_redirects must be non-negative")
        self._allowed = frozenset(host.strip().lower() for host in allowed_hosts)
        self._default_timeout = default_timeout_seconds
        self._max_redirects = max_redirects
        self._client_factory = client_factory
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def fetch(
        self,
        ctx: ToolContext,
        *,
        url: str,
        timeout_seconds: float | None = None,
    ) -> WebFetchOutcome:
        hostname = self._validate_url(url)
        self._check_host_allowed(hostname)
        cap = max(ctx.output_byte_cap, 0)
        client = await self._ensure_client()
        timeout = timeout_seconds if timeout_seconds is not None else self._default_timeout
        if timeout <= 0:
            raise ToolValidationError("timeout_seconds must be positive")
        try:
            response = await client.get(url, timeout=timeout)
        except httpx.TimeoutException as exc:
            raise ToolExecutionError(f"web_fetch: request timed out after {timeout}s") from exc
        except httpx.HTTPError as exc:
            raise ToolExecutionError(f"web_fetch: network error: {exc!s}") from exc
        return self._build_outcome(response, cap)

    async def close(self) -> None:
        async with self._lock:
            if self._client is not None:
                await self._client.aclose()
                self._client = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        async with self._lock:
            if self._client is None:
                if self._client_factory is not None:
                    factory_fn = self._client_factory
                    self._client = factory_fn()  # type: ignore[operator]
                else:
                    self._client = httpx.AsyncClient(
                        follow_redirects=True,
                        max_redirects=self._max_redirects,
                    )
            return self._client

    def _validate_url(self, url: str) -> str:
        if not url or not isinstance(url, str):
            raise ToolValidationError("web_fetch: url must be a non-empty str")
        try:
            parsed = urlparse(url)
        except ValueError as exc:
            raise ToolValidationError(f"web_fetch: invalid url {url!r}: {exc!s}") from exc
        if parsed.scheme not in ("http", "https"):
            raise ToolValidationError(
                f"web_fetch: scheme {parsed.scheme!r} not allowed; use http(s)"
            )
        if not parsed.hostname:
            raise ToolValidationError(f"web_fetch: url {url!r} has no hostname")
        return parsed.hostname

    def _check_host_allowed(self, host: str) -> None:
        normalised = host.lower()
        for allowed in self._allowed:
            # Suffix match with a dot boundary so ``docs.example.com``
            # matches an entry of ``example.com`` but ``evilexample.com``
            # does not match ``example.com``.
            if normalised == allowed or normalised.endswith("." + allowed):
                return
        raise ToolPermissionError(
            f"web_fetch: hostname {host!r} not in allow-list {sorted(self._allowed)!r}"
        )

    def _build_outcome(self, response: httpx.Response, cap: int) -> WebFetchOutcome:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if not _is_text_content_type(content_type):
            raise ToolValidationError(
                f"web_fetch: content-type {content_type!r} is not text; refusing to surface binary"
            )
        body_bytes = response.content
        truncated = cap > 0 and len(body_bytes) > cap
        if truncated:
            body_bytes = body_bytes[:cap]
        try:
            body_text = body_bytes.decode(
                response.encoding or response.charset_encoding or "utf-8",
                errors="replace",
            )
        except (LookupError, UnicodeDecodeError):
            body_text = body_bytes.decode("utf-8", errors="replace")
        return WebFetchOutcome(
            final_url=str(response.url),
            status=response.status_code,
            content_type=content_type or "application/octet-stream",
            content=body_text,
            truncated=truncated,
            bytes_received=len(response.content),
        )


def _is_text_content_type(content_type: str) -> bool:
    if not content_type:
        # Servers occasionally omit Content-Type; assume text rather
        # than refuse outright — many docs servers behave this way and
        # the body decode step will fall back to UTF-8 replace.
        return True
    return any(content_type.startswith(prefix) for prefix in _TEXT_CONTENT_TYPE_PREFIXES)
