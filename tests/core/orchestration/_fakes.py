"""Test doubles shared across orchestration unit tests."""

from __future__ import annotations

from collections.abc import Callable

from meta_agent.core.orchestration.deps import GraphDeps
from meta_agent.core.ports.llm import (
    FinishReason,
    LLMClient,
    LLMRequest,
    LLMResponse,
    LLMUsage,
)


class FakeLLMClient(LLMClient):
    """In-memory :class:`LLMClient` used by graph unit tests.

    The client records every received request and returns either a
    pre-canned response or whatever ``handler`` produces. Pass an
    exception-raising handler to exercise error paths.
    """

    def __init__(
        self,
        *,
        response: LLMResponse | None = None,
        handler: Callable[[LLMRequest], LLMResponse] | None = None,
    ) -> None:
        if response is None and handler is None:
            response = LLMResponse(
                content="ok",
                model="fake/echo",
                finish_reason="stop",
                usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        self._response = response
        self._handler = handler
        self.calls: list[LLMRequest] = []
        self.closed = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if self._handler is not None:
            return self._handler(request)
        assert self._response is not None  # default branch populated above
        return self._response

    async def close(self) -> None:
        self.closed = True


def make_response(
    *,
    content: str = "ok",
    model: str = "fake/echo",
    finish_reason: FinishReason = "stop",
    usage: LLMUsage | None = None,
    provider_response_id: str | None = None,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        model=model,
        finish_reason=finish_reason,
        usage=usage or LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        provider_response_id=provider_response_id,
    )


def fake_deps(
    client: LLMClient | None = None,
    *,
    git_push_token: str | None = None,
) -> GraphDeps:
    """Build a :class:`GraphDeps` with an opinionated :class:`FakeLLMClient`."""

    return GraphDeps(
        llm=client if client is not None else FakeLLMClient(),
        git_push_token=git_push_token,
    )
