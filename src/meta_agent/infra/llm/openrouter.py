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
import json
import logging
from collections.abc import AsyncIterator
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx

from meta_agent.core.ports.llm import (
    ChatMessage,
    FinishReason,
    LLMAuthError,
    LLMClient,
    LLMError,
    LLMInvalidRequestError,
    LLMRateLimitedError,
    LLMRequest,
    LLMResponse,
    LLMStreamChunk,
    LLMTransientError,
    LLMUsage,
    MessageRole,
    ToolCallDelta,
)
from meta_agent.core.ports.tools import ToolCall, ToolSpec
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
    missing the ``choices[0].message`` path so the retry loop can
    attempt again rather than crashing the caller. ``content`` may be
    ``null`` when the model elected to invoke tools instead of producing
    text; in that case we surface it as the empty string and rely on
    :attr:`LLMResponse.tool_calls` to carry the action.
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
    raw_content = message.get("content")
    if raw_content is None:
        reasoning_content = message.get("reasoning")
        content = reasoning_content if isinstance(reasoning_content, str) else ""
    elif isinstance(raw_content, str):
        content = raw_content
    else:
        raise LLMTransientError("openrouter choice content is not string|null")
    tool_calls = _decode_tool_calls(message.get("tool_calls"))
    if not content and not tool_calls:
        raise LLMTransientError("openrouter choice has neither content nor tool_calls")
    usage_value = body.get("usage")
    usage_raw: dict[str, Any] = usage_value if isinstance(usage_value, dict) else {}
    usage = LLMUsage(
        prompt_tokens=_maybe_int(usage_raw.get("prompt_tokens")),
        completion_tokens=_maybe_int(usage_raw.get("completion_tokens")),
        total_tokens=_maybe_int(usage_raw.get("total_tokens")),
        cost_usd_micros=_maybe_cost_usd_micros(usage_raw.get("cost")),
    )
    model_id = body.get("model")
    return LLMResponse(
        content=content,
        model=model_id if isinstance(model_id, str) else "",
        finish_reason=_coerce_finish_reason(first.get("finish_reason")),
        usage=usage,
        tool_calls=tool_calls,
        provider_response_id=body.get("id") if isinstance(body.get("id"), str) else None,
    )


def _decode_tool_calls(raw: object) -> tuple[ToolCall, ...]:
    """Parse an OpenAI-style ``tool_calls`` array into typed objects.

    Unknown / malformed entries raise :class:`LLMTransientError` so
    retry kicks in rather than silently dropping a model action. Each
    entry's ``function.arguments`` is a JSON-encoded string per the
    upstream contract; we decode it eagerly into ``dict[str, Any]``.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise LLMTransientError("openrouter tool_calls is not an array")
    decoded: list[ToolCall] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise LLMTransientError("openrouter tool_call entry is not an object")
        call_id = entry.get("id")
        function = entry.get("function")
        if not isinstance(call_id, str) or not call_id:
            raise LLMTransientError("openrouter tool_call missing id")
        if not isinstance(function, dict):
            raise LLMTransientError("openrouter tool_call missing function payload")
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise LLMTransientError("openrouter tool_call function missing name")
        arguments_raw = function.get("arguments", "{}")
        if not isinstance(arguments_raw, str):
            raise LLMTransientError("openrouter tool_call arguments must be a JSON string")
        try:
            arguments = json.loads(arguments_raw) if arguments_raw else {}
        except ValueError as exc:
            raise LLMTransientError(
                f"openrouter tool_call arguments not valid JSON: {exc!s}"
            ) from exc
        if not isinstance(arguments, dict):
            raise LLMTransientError("openrouter tool_call arguments must decode to object")
        decoded.append(ToolCall(id=call_id, name=name, arguments=arguments))
    return tuple(decoded)


def _decode_stream_event(event: dict[str, Any]) -> LLMStreamChunk | None:
    """Translate one OpenRouter SSE event into a typed stream chunk.

    Returns ``None`` for events that carry no observable state — some
    providers emit keep-alive heartbeats with an empty ``choices``
    list and no usage. Malformed events raise :class:`LLMTransientError`
    so the caller surfaces a structured error rather than truncating
    the stream silently.
    """
    choices = event.get("choices")
    content_delta = ""
    tool_call_deltas: tuple[ToolCallDelta, ...] = ()
    finish_reason: FinishReason | None = None
    if isinstance(choices, list) and choices:
        first = choices[0]
        if not isinstance(first, dict):
            raise LLMTransientError("openrouter stream choice not an object")
        delta = first.get("delta")
        if delta is not None:
            if not isinstance(delta, dict):
                raise LLMTransientError("openrouter stream delta not an object")
            raw_content = delta.get("content")
            if isinstance(raw_content, str):
                content_delta = raw_content
            elif raw_content is not None:
                raise LLMTransientError("openrouter stream content not string|null")
            tool_call_deltas = _decode_tool_call_deltas(delta.get("tool_calls"))
        finish_raw = first.get("finish_reason")
        if isinstance(finish_raw, str):
            finish_reason = _coerce_finish_reason(finish_raw)

    usage_value = event.get("usage")
    usage: LLMUsage | None = None
    if isinstance(usage_value, dict):
        usage = LLMUsage(
            prompt_tokens=_maybe_int(usage_value.get("prompt_tokens")),
            completion_tokens=_maybe_int(usage_value.get("completion_tokens")),
            total_tokens=_maybe_int(usage_value.get("total_tokens")),
            cost_usd_micros=_maybe_cost_usd_micros(usage_value.get("cost")),
        )

    model_value = event.get("model")
    model_id = model_value if isinstance(model_value, str) else None

    response_id_value = event.get("id")
    response_id = response_id_value if isinstance(response_id_value, str) else None

    if (
        not content_delta
        and not tool_call_deltas
        and finish_reason is None
        and usage is None
        and model_id is None
        and response_id is None
    ):
        return None

    return LLMStreamChunk(
        content_delta=content_delta,
        tool_call_deltas=tool_call_deltas,
        finish_reason=finish_reason,
        usage=usage,
        model=model_id,
        provider_response_id=response_id,
    )


def _decode_tool_call_deltas(raw: object) -> tuple[ToolCallDelta, ...]:
    """Parse OpenAI-style streaming ``tool_calls`` delta array.

    Unlike the non-streaming counterpart in :func:`_decode_tool_calls`,
    ``id`` / ``name`` are optional in streaming chunks (they typically
    appear only on the first delta of each tool call), and
    ``arguments`` is a partial JSON string fragment — callers
    reassemble per-index before parsing.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise LLMTransientError("openrouter stream tool_calls not an array")
    decoded: list[ToolCallDelta] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise LLMTransientError("openrouter stream tool_call entry not an object")
        idx_raw = entry.get("index")
        if isinstance(idx_raw, bool) or not isinstance(idx_raw, int) or idx_raw < 0:
            raise LLMTransientError("openrouter stream tool_call missing valid index")
        call_id_raw = entry.get("id")
        if call_id_raw is not None and not isinstance(call_id_raw, str):
            raise LLMTransientError("openrouter stream tool_call id not string|null")
        function = entry.get("function")
        name: str | None = None
        arguments_delta = ""
        if function is not None:
            if not isinstance(function, dict):
                raise LLMTransientError("openrouter stream tool_call function not an object")
            name_raw = function.get("name")
            if name_raw is not None:
                if not isinstance(name_raw, str):
                    raise LLMTransientError("openrouter stream tool_call name not string|null")
                name = name_raw
            args_raw = function.get("arguments")
            if args_raw is not None:
                if not isinstance(args_raw, str):
                    raise LLMTransientError("openrouter stream tool_call arguments not a string")
                arguments_delta = args_raw
        decoded.append(
            ToolCallDelta(
                index=idx_raw,
                id=call_id_raw,
                name=name,
                arguments_delta=arguments_delta,
            )
        )
    return tuple(decoded)


def _maybe_int(value: object) -> int | None:
    """Return ``value`` as ``int`` when possible, else ``None``."""
    if isinstance(value, bool):  # bool is subclass of int; reject explicitly
        return None
    if isinstance(value, int):
        return value
    return None


def _maybe_cost_usd_micros(value: object) -> int | None:
    """Return a provider cost value as integer USD micro-units.

    OpenRouter reports ``usage.cost`` as account credits. The project
    stores monetary usage in micro-USD integers; OpenRouter credits are
    treated as USD-equivalent for this raw provider log.
    """

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float | str):
        try:
            cost = Decimal(str(value))
        except InvalidOperation:
            return None
        if cost < 0:
            return None
        micros = (cost * Decimal("1000000")).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return int(micros)
    return None


def _encode_message(message: ChatMessage) -> dict[str, Any]:
    """Encode a :class:`ChatMessage` in OpenAI/OpenRouter wire shape.

    Tool-role messages carry ``tool_call_id``; assistant messages with
    ``tool_calls`` emit them under ``tool_calls`` (each function's
    ``arguments`` is serialised as a JSON string per the spec).
    """
    encoded: dict[str, Any] = {"role": message.role.value, "content": message.content}
    if message.role is MessageRole.TOOL:
        if not message.tool_call_id:
            raise LLMInvalidRequestError("tool-role messages require tool_call_id")
        encoded["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        encoded["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, sort_keys=True),
                },
            }
            for call in message.tool_calls
        ]
    return encoded


def _encode_tool_spec(spec: ToolSpec) -> dict[str, Any]:
    """Encode a :class:`ToolSpec` as an OpenAI-style ``function`` tool."""
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": dict(spec.parameters),
        },
    }


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

    async def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamChunk]:
        """Open a streaming chat completion against OpenRouter.

        Retries the *initial* connection on transient transport / 5xx
        failures (using the same backoff policy as :meth:`complete`).
        Once the first chunk has been yielded to the caller, retry is
        no longer safe — mid-stream failures surface as
        :class:`LLMTransientError` to the consumer.

        The async generator owns the underlying ``httpx`` streaming
        response via ``async with``; closing the generator early
        (caller ``break``s out of ``async for``) releases the HTTP
        resources via the standard async-generator finalisation
        protocol.
        """
        body = self._build_body(request)
        body["stream"] = True
        attempts = self._config.max_retries + 1
        last_error: LLMError | None = None

        for attempt in range(attempts):
            retry = False
            chunks_yielded = False
            try:
                async with self._client.stream("POST", "/chat/completions", json=body) as response:
                    if response.status_code != 200:
                        await response.aread()
                        try:
                            self._parse(response)
                        except LLMTransientError as exc:
                            last_error = exc
                            retry = True
                    else:
                        async for chunk in self._iter_sse_chunks(response):
                            chunks_yielded = True
                            yield chunk
                        return
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if chunks_yielded:
                    raise LLMTransientError(f"openrouter stream interrupted: {exc!s}") from exc
                last_error = LLMTransientError(f"openrouter transport: {exc!s}")
                retry = True
                logger.warning(
                    "llm.openrouter.stream_transport_error",
                    extra={"attempt": attempt + 1, "error_type": type(exc).__name__},
                )

            if not retry:
                break
            assert last_error is not None  # retry=True always sets last_error
            await self._maybe_backoff(attempt, attempts, last_error)

        assert last_error is not None
        raise last_error

    async def _iter_sse_chunks(self, response: httpx.Response) -> AsyncIterator[LLMStreamChunk]:
        """Parse OpenAI-compatible SSE events from a streaming response body.

        Each event is a ``data: {json}`` line terminated by an empty
        line; the sentinel ``data: [DONE]`` closes the stream. Lines
        that are blank, prefixed with comments (``:``), or carry
        non-data SSE fields are skipped per the SSE spec. JSON decode
        failures surface as :class:`LLMTransientError`.
        """
        async for line in response.aiter_lines():
            if not line:
                continue
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload:
                continue
            if payload == "[DONE]":
                return
            try:
                event = json.loads(payload)
            except ValueError as exc:
                raise LLMTransientError(f"openrouter stream invalid JSON: {exc!s}") from exc
            if not isinstance(event, dict):
                raise LLMTransientError("openrouter stream event not an object")
            chunk = _decode_stream_event(event)
            if chunk is not None:
                yield chunk

    async def close(self) -> None:
        await self._client.aclose()

    def _build_body(self, request: LLMRequest) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": request.model or self._config.default_model,
            "messages": [_encode_message(m) for m in request.messages],
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_tokens is not None:
            body["max_tokens"] = request.max_tokens
        if request.stop:
            body["stop"] = list(request.stop)
        if request.tools:
            body["tools"] = [_encode_tool_spec(spec) for spec in request.tools]
        if self._config.exclude_reasoning:
            body["reasoning"] = {"exclude": True}
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
