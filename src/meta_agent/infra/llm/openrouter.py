"""OpenRouter HTTP adapter for the :class:`LLMClient` port.

OpenRouter exposes an OpenAI-compatible Chat Completions surface
(`POST /chat/completions`). The adapter translates the typed port
contract into that surface, normalises the response, and maps HTTP /
transport failures into the :mod:`meta_agent.core.ports.llm` error
taxonomy.

Secret handling: the API key is bound at construction time and never
logged. Request / response payloads are not logged in full either —
only sizes, model id, and finish reason — to avoid leaking prompts.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from meta_agent.core.ports.llm import (
    FinishReason,
    LLMAuthError,
    LLMClient,
    LLMError,
    LLMInvalidRequestError,
    LLMRateLimitedError,
    LLMRequest,
    LLMResponse,
    LLMTransientError,
    LLMUsage,
)
from meta_agent.infra.llm.config import OpenRouterConfig

logger = logging.getLogger(__name__)

_VALID_FINISH: tuple[FinishReason, ...] = (
    "stop",
    "length",
    "content_filter",
    "tool_call",
    "other",
)
_FORBIDDEN_HEADER_SUBSTRINGS = ("token", "key", "secret", "authorization")


def _sanitize_extra_headers(extra: dict[str, str]) -> dict[str, str]:
    """Strip auth-shaped keys from caller-supplied headers."""
    out: dict[str, str] = {}
    for k, v in extra.items():
        lower = k.lower()
        if any(s in lower for s in _FORBIDDEN_HEADER_SUBSTRINGS):
            continue
        out[k] = v
    return out


def _coerce_finish_reason(raw: object) -> FinishReason:
    """Map provider-specific finish reasons onto the port's literal set."""
    if isinstance(raw, str):
        for valid in _VALID_FINISH:
            if raw == valid:
                return valid
        if raw == "tool_calls":
            return "tool_call"
    return "other"


def _decode_success(response: httpx.Response) -> LLMResponse:
    """Translate a 200 response into an :class:`LLMResponse`.

    Raises :class:`LLMTransientError` if the body is unparseable or
    missing the ``choices[0].message.content`` path so the retry loop
    can attempt again rather than crashing the caller.
    """
    try:
        body = response.json()
    except ValueError as exc:
        raise LLMTransientError(f"openrouter returned invalid JSON: {exc!s}") from exc
    if not isinstance(body, dict):
        raise LLMTransientError("openrouter response not a JSON object")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMTransientError("openrouter response missing choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise LLMTransientError("openrouter choice is not an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise LLMTransientError("openrouter choice missing message")
    content = message.get("content")
    if not isinstance(content, str):
        raise LLMTransientError("openrouter choice missing string content")
    usage_value = body.get("usage")
    usage_raw: dict[str, Any] = usage_value if isinstance(usage_value, dict) else {}
    usage = LLMUsage(
        prompt_tokens=_maybe_int(usage_raw.get("prompt_tokens")),
        completion_tokens=_maybe_int(usage_raw.get("completion_tokens")),
        total_tokens=_maybe_int(usage_raw.get("total_tokens")),
    )
    model_id = body.get("model")
    return LLMResponse(
        content=content,
        model=model_id if isinstance(model_id, str) else "",
        finish_reason=_coerce_finish_reason(first.get("finish_reason")),
        usage=usage,
        provider_response_id=body.get("id") if isinstance(body.get("id"), str) else None,
    )


def _maybe_int(value: object) -> int | None:
    """Return ``value`` as ``int`` when possible, else ``None``."""
    if isinstance(value, bool):  # bool is subclass of int; reject explicitly
        return None
    if isinstance(value, int):
        return value
    return None


class OpenRouterClient(LLMClient):
    """LLM adapter targeting `https://openrouter.ai/api/v1/chat/completions`."""

    def __init__(
        self,
        config: OpenRouterConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Any = None,
    ) -> None:
        if not config.api_key:
            raise ValueError("OpenRouterConfig.api_key is required")
        self._config = config
        self._sleep = sleep if sleep is not None else asyncio.sleep
        headers: dict[str, str] = {
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        }
        if config.referer:
            headers["HTTP-Referer"] = config.referer
        if config.title:
            headers["X-Title"] = config.title
        headers.update(_sanitize_extra_headers(config.extra_headers))
        self._client = httpx.AsyncClient(
            base_url=config.base_url,
            headers=headers,
            timeout=httpx.Timeout(config.timeout_seconds),
            transport=transport,
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        body = self._build_body(request)
        attempts = self._config.max_retries + 1
        last_error: LLMError | None = None
        for attempt in range(attempts):
            try:
                response = await self._client.post("/chat/completions", json=body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = LLMTransientError(f"openrouter transport: {exc!s}")
                logger.warning(
                    "llm.openrouter.transport_error",
                    extra={"attempt": attempt + 1, "error_type": type(exc).__name__},
                )
                await self._maybe_backoff(attempt, attempts, last_error)
                continue
            try:
                parsed = self._parse(response)
            except LLMTransientError as exc:
                last_error = exc
                await self._maybe_backoff(attempt, attempts, last_error)
                continue
            return parsed
        assert last_error is not None  # exhausted attempts
        raise last_error

    async def close(self) -> None:
        await self._client.aclose()

    def _build_body(self, request: LLMRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model or self._config.default_model,
            "messages": [{"role": m.role.value, "content": m.content} for m in request.messages],
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.stop:
            body["stop"] = list(request.stop)
        return body

    def _parse(self, response: httpx.Response) -> LLMResponse:
        status = response.status_code
        if status == 200:
            return _decode_success(response)
        if status == 401 or status == 403:
            raise LLMAuthError(f"openrouter auth failed: {status}")
        if status == 429:
            retry_after_raw = response.headers.get("retry-after")
            retry_after: float | None = None
            if retry_after_raw is not None:
                try:
                    retry_after = float(retry_after_raw)
                except ValueError:
                    retry_after = None
            raise LLMRateLimitedError(f"openrouter rate limited: {status}", retry_after=retry_after)
        if 400 <= status < 500:
            raise LLMInvalidRequestError(f"openrouter rejected request: {status}")
        if 500 <= status < 600:
            raise LLMTransientError(f"openrouter upstream error: {status}")
        raise LLMError(f"openrouter unexpected status: {status}")

    async def _maybe_backoff(self, attempt: int, attempts: int, error: LLMError) -> None:
        if attempt + 1 >= attempts:
            return
        if isinstance(error, LLMRateLimitedError) and error.retry_after is not None:
            delay = min(error.retry_after, self._config.max_backoff_seconds)
        else:
            base = self._config.initial_backoff_seconds * (2**attempt)
            delay = min(base, self._config.max_backoff_seconds)
        await self._sleep(delay)
