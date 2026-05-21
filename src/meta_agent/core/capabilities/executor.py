"""Tool executor seam: turn a ``ToolCall`` into a bounded ``ToolResult``.

The executor sits between the agent loop and the :class:`ToolRegistry`.
Graph nodes hand it a :class:`ToolCall` emitted by the LLM and get
back a :class:`ToolResult` they can append to the conversation as a
tool-role message. The executor encapsulates three concerns the
graph layer should not have to repeat:

1. **Name dispatch.** Resolves the call to a registered handler;
   unknown names short-circuit into a uniformly-shaped error
   observation rather than raising.
2. **Error normalisation.** Catches every :class:`ToolError` raised
   by the handler and turns it into ``ToolResult(is_error=True)`` so
   the LLM can observe the failure and self-correct. Unexpected
   exceptions still propagate up so genuine bugs are not silently
   swallowed.
3. **Output bounding.** Truncates ``content`` to
   ``ctx.output_byte_cap`` (or the executor-level ``max_result_bytes``
   ceiling, whichever is smaller) on a UTF-8-safe boundary. The
   ``truncated`` flag records when the cap was hit so callers can
   surface that to the user. This is the single chokepoint that keeps
   one runaway tool from blowing up the conversation context.
"""

from __future__ import annotations

from meta_agent.core.capabilities.registry import ToolRegistry
from meta_agent.core.ports.tools import (
    ToolCall,
    ToolContext,
    ToolError,
    ToolNotFoundError,
    ToolResult,
)

_DEFAULT_MAX_RESULT_BYTES = 65536


def _truncate_utf8(text: str, max_bytes: int) -> tuple[str, bool]:
    """Truncate ``text`` to at most ``max_bytes`` UTF-8 bytes.

    Returns ``(possibly_truncated_text, was_truncated)``. The cut is
    placed on a code-point boundary so callers never receive invalid
    UTF-8. ``max_bytes`` ``<= 0`` is treated as ``0`` (everything is
    truncated away); callers should validate caps before invoking.
    """

    if max_bytes <= 0:
        return ("", bool(text))
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return (text, False)
    # ``decode(errors="ignore")`` discards the incomplete final
    # code point at the cut, leaving a clean UTF-8 prefix.
    return (encoded[:max_bytes].decode("utf-8", errors="ignore"), True)


class ToolExecutor:
    """Dispatch :class:`ToolCall` instances against a :class:`ToolRegistry`.

    ``max_result_bytes`` caps the *executor-side* output size; a
    handler that returns more than this gets its ``content`` truncated
    and the ``truncated`` flag flipped on the resulting
    :class:`ToolResult`. The per-call ``ctx.output_byte_cap`` further
    narrows the cap on a per-invocation basis (the effective cap is
    the minimum of the two), so a graph can shrink the cap mid-loop
    without re-instantiating the executor.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        max_result_bytes: int = _DEFAULT_MAX_RESULT_BYTES,
    ) -> None:
        if max_result_bytes <= 0:
            raise ValueError("max_result_bytes must be positive")
        self._registry = registry
        self._max_result_bytes = max_result_bytes

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        """Run ``call`` against the registry and return a bounded result.

        The contract:

        * Unknown tool name → ``ToolResult(is_error=True)`` with the
          :class:`ToolNotFoundError` message as content.
        * Any :class:`ToolError` raised by the handler →
          ``ToolResult(is_error=True)`` with ``str(exc)`` as content.
        * Any other exception propagates: the graph node treats it as
          a hard failure, never as a self-correctable observation.
        * On success or normalised error, the ``content`` is truncated
          to ``min(max_result_bytes, ctx.output_byte_cap)`` UTF-8 bytes.
        """

        cap = min(self._max_result_bytes, ctx.output_byte_cap)
        try:
            registered = self._registry.get(call.name)
        except ToolNotFoundError as exc:
            return self._error_result(call, str(exc), cap)

        try:
            result = await registered.handler(call, ctx)
        except ToolError as exc:
            return self._error_result(call, str(exc), cap)

        return self._bound(result, cap)

    @staticmethod
    def _error_result(call: ToolCall, message: str, cap: int) -> ToolResult:
        content, truncated = _truncate_utf8(message, cap)
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content=content,
            is_error=True,
            truncated=truncated,
        )

    @staticmethod
    def _bound(result: ToolResult, cap: int) -> ToolResult:
        content, truncated = _truncate_utf8(result.content, cap)
        if not truncated and content == result.content:
            return result
        return result.model_copy(
            update={"content": content, "truncated": result.truncated or truncated}
        )
