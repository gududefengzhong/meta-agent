"""Test doubles shared across orchestration unit tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from meta_agent.core.capabilities.executor import ToolExecutor
from meta_agent.core.capabilities.registry import ToolRegistry
from meta_agent.core.domain.prompt_asset import PromptAsset
from meta_agent.core.orchestration.deps import GraphDeps
from meta_agent.core.ports.llm import (
    FinishReason,
    LLMClient,
    LLMRequest,
    LLMResponse,
    LLMUsage,
)
from meta_agent.core.ports.permission_gate import PermissionGate
from meta_agent.core.ports.prompt_registry import PromptRegistry
from meta_agent.core.ports.tools import ToolCall
from meta_agent.infra.prompt_registry.in_memory import InMemoryPromptRegistry
from meta_agent.infra.prompt_registry.seeds import BUILTIN_PROMPT_SEEDS


class FakeLLMClient(LLMClient):
    """In-memory :class:`LLMClient` used by graph unit tests.

    The client records every received request and returns either a
    pre-canned response, a script of responses (one per call), or
    whatever ``handler`` produces. Pass an exception-raising handler to
    exercise error paths.
    """

    def __init__(
        self,
        *,
        response: LLMResponse | None = None,
        responses: list[LLMResponse] | None = None,
        handler: Callable[[LLMRequest], LLMResponse] | None = None,
    ) -> None:
        if response is None and responses is None and handler is None:
            response = LLMResponse(
                content="ok",
                model="fake/echo",
                finish_reason="stop",
                usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )
        self._response = response
        self._responses = list(responses) if responses is not None else None
        self._handler = handler
        self.calls: list[LLMRequest] = []
        self.closed = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        if self._handler is not None:
            return self._handler(request)
        if self._responses is not None:
            if not self._responses:
                raise AssertionError("FakeLLMClient.responses script exhausted")
            return self._responses.pop(0)
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
    tool_calls: tuple[ToolCall, ...] = (),
    provider_response_id: str | None = None,
) -> LLMResponse:
    return LLMResponse(
        content=content,
        model=model,
        finish_reason=finish_reason,
        usage=usage or LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        tool_calls=tool_calls,
        provider_response_id=provider_response_id,
    )


_SEED_TIMESTAMP = datetime(2026, 5, 22, tzinfo=UTC)


def make_seeded_prompt_registry() -> PromptRegistry:
    """Return an :class:`InMemoryPromptRegistry` populated with built-in seeds.

    The production ``ensure_seeded`` helper is async; this synchronous
    variant exists only to keep ``fake_deps()`` callable from sync
    setup code without dragging an event loop into the construction
    path. The resulting registry is functionally identical for the
    purposes of unit tests.
    """

    registry = InMemoryPromptRegistry()
    for seed in BUILTIN_PROMPT_SEEDS:
        registry._rows.append(
            PromptAsset(
                prompt_id=seed.prompt_id,
                version=1,
                tenant_id=None,
                content=seed.content,
                description=seed.description,
                created_at=_SEED_TIMESTAMP,
            )
        )
    return registry


def fake_deps(
    client: LLMClient | None = None,
    *,
    git_push_token: str | None = None,
    tool_registry: ToolRegistry | None = None,
    tool_executor: ToolExecutor | None = None,
    prompt_registry: PromptRegistry | None = None,
    permission_gate: PermissionGate | None = None,
) -> GraphDeps:
    """Build a :class:`GraphDeps` with an opinionated :class:`FakeLLMClient`.

    When ``tool_registry`` is supplied but ``tool_executor`` is not,
    a default :class:`ToolExecutor` is materialised against it so
    callers can write ``fake_deps(client, tool_registry=reg)`` without
    constructing the executor by hand.

    ``prompt_registry`` defaults to an :class:`InMemoryPromptRegistry`
    pre-seeded with every built-in prompt (matching what
    :func:`meta_agent.worker.bootstrap.build_registry` does at boot).
    Tests that want to exercise the "missing registry" path can pass
    a sentinel via :class:`dataclasses.replace` after construction.

    ``permission_gate`` defaults to ``None`` (no interactive gate);
    tests that exercise APPROVE_EACH_TOOL pass an explicit gate.
    """

    if tool_registry is not None and tool_executor is None:
        tool_executor = ToolExecutor(tool_registry)
    if prompt_registry is None:
        prompt_registry = make_seeded_prompt_registry()
    return GraphDeps(
        llm=client if client is not None else FakeLLMClient(),
        git_push_token=git_push_token,
        tool_registry=tool_registry,
        tool_executor=tool_executor,
        prompt_registry=prompt_registry,
        permission_gate=permission_gate,
    )
