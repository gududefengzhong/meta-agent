"""Unit tests for the ``builtin.shell_agent`` graph."""

from __future__ import annotations

from pathlib import Path

import pytest

from meta_agent.core.capabilities.registry import ToolRegistry
from meta_agent.core.domain.audit import AuditEvent
from meta_agent.core.orchestration import TaskRunState
from meta_agent.core.orchestration.graphs.shell_agent import (
    SHELL_AGENT_GRAPH_ID,
    build_shell_agent_graph,
)
from meta_agent.core.ports.audit_sink import AuditSink
from meta_agent.core.ports.llm import LLMUsage
from meta_agent.core.ports.tools import (
    ToolCall,
    ToolCategory,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from tests.core.orchestration._fakes import FakeLLMClient, fake_deps, make_response

pytestmark = pytest.mark.asyncio


class _AuditSpy(AuditSink):
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    async def append(self, event: AuditEvent) -> None:
        self.events.append(event)


def _state(**data: object) -> TaskRunState:
    return TaskRunState(
        task_id="task-1",
        tenant_id="tenant-1",
        trace_id="trace-1",
        graph_id=SHELL_AGENT_GRAPH_ID,
        data=data,
    )


def _spec(name: str) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="d",
        parameters={"type": "object"},
        category=ToolCategory.FILESYSTEM,
    )


def _registry_with(
    name: str, *, content: str = "tool-output"
) -> tuple[ToolRegistry, list[ToolCall]]:
    """Build a registry whose single tool records every call and returns ``content``."""
    registry = ToolRegistry()
    recorded: list[ToolCall] = []

    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        recorded.append(call)
        return ToolResult(call_id=call.id, name=call.name, content=content)

    registry.register(_spec(name), handler)
    return registry, recorded


async def test_loop_terminates_when_llm_returns_no_tool_calls() -> None:
    client = FakeLLMClient(response=make_response(content="final", model="fake/m1"))
    registry, _ = _registry_with("noop")
    deps = fake_deps(client, tool_registry=registry)
    graph = build_shell_agent_graph(deps)

    final = await graph.run(_state(user_prompt="hi"))

    assert final.finished is True
    output = final.data["output"]
    assert isinstance(output, dict)
    assert output["assistant_message"] == "final"
    assert output["steps"] == 1
    assert output["tool_invocations"] == 0
    assert output["truncated_by_max_steps"] is False
    assert len(client.calls) == 1


async def test_tool_call_executes_and_observation_feeds_next_plan(tmp_path: Path) -> None:
    registry, recorded = _registry_with("fs_read", content="hello")
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(ToolCall(id="c1", name="fs_read", arguments={"path": "x"}),),
                finish_reason="tool_call",
            ),
            make_response(content="all done", finish_reason="stop"),
        ]
    )
    deps = fake_deps(client, tool_registry=registry)
    graph = build_shell_agent_graph(deps)

    final = await graph.run(_state(user_prompt="hi", _workspace_path=str(tmp_path)))

    assert final.finished is True
    output = final.data["output"]
    assert output["assistant_message"] == "all done"  # type: ignore[index]
    assert output["steps"] == 2  # type: ignore[index]
    assert output["tool_invocations"] == 1  # type: ignore[index]
    assert len(recorded) == 1
    assert recorded[0].name == "fs_read"
    # second LLM call should have included the tool observation
    second_request = client.calls[1]
    roles = [m.role.value for m in second_request.messages]
    assert "tool" in roles
    tool_msg = second_request.messages[-1]
    assert "tool_metadata" not in tool_msg.content


async def test_tool_call_emits_structured_audit_events(tmp_path: Path) -> None:
    registry, _ = _registry_with("fs_read", content="hello")
    audit = _AuditSpy()
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(ToolCall(id="c1", name="fs_read", arguments={"path": "x"}),),
                finish_reason="tool_call",
            ),
            make_response(content="all done", finish_reason="stop"),
        ]
    )
    deps = fake_deps(client, tool_registry=registry, audit_sink=audit)
    graph = build_shell_agent_graph(deps)

    await graph.run(_state(user_prompt="hi", _workspace_path=str(tmp_path)))

    actions = [event.action for event in audit.events]
    assert actions == ["tool.invoked", "tool.completed"]
    invoked = audit.events[0]
    assert invoked.tenant_id == "tenant-1"
    assert invoked.task_id == "task-1"
    assert invoked.trace_id == "trace-1"
    assert invoked.payload["tool_name"] == "fs_read"
    assert invoked.payload["agent_step"] == 1
    assert invoked.payload["arguments"] == {"path": "x"}
    completed = audit.events[1]
    assert completed.payload["output_bytes"] == 5
    assert completed.payload["truncated"] is False


async def test_tool_observation_preserves_metadata_and_truncation_signals(tmp_path: Path) -> None:
    registry = ToolRegistry()

    async def handler(call: ToolCall, ctx: ToolContext) -> ToolResult:
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content="abcdef",
            truncated=True,
            metadata={"bytes_written": "6"},
        )

    registry.register(_spec("edit_write"), handler)
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(ToolCall(id="c1", name="edit_write", arguments={}),),
                finish_reason="tool_call",
            ),
            make_response(content="done", finish_reason="stop"),
        ]
    )
    deps = fake_deps(client, tool_registry=registry)
    graph = build_shell_agent_graph(deps)

    await graph.run(_state(user_prompt="hi", _workspace_path=str(tmp_path)))

    tool_msg = client.calls[1].messages[-1]
    assert tool_msg.role.value == "tool"
    assert "tool_output_truncated=true" in tool_msg.content
    assert 'tool_metadata={"bytes_written": "6"}' in tool_msg.content
    assert "abcdef" in tool_msg.content


async def test_unknown_tool_surfaces_is_error_observation_then_completes() -> None:
    registry, _ = _registry_with("fs_read")
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(ToolCall(id="c1", name="nope", arguments={}),),
                finish_reason="tool_call",
            ),
            make_response(content="gave up", finish_reason="stop"),
        ]
    )
    deps = fake_deps(client, tool_registry=registry)
    graph = build_shell_agent_graph(deps)

    final = await graph.run(_state(user_prompt="hi"))

    assert final.finished is True
    # second LLM call's last tool message should be the is_error normalisation
    second_request = client.calls[1]
    tool_msg = second_request.messages[-1]
    assert tool_msg.role.value == "tool"
    assert "tool_status=error" in tool_msg.content
    assert "nope" in tool_msg.content


async def test_max_steps_cap_short_circuits_loop() -> None:
    registry, _ = _registry_with("fs_read")
    # LLM keeps demanding a tool call; after ``max_steps`` plan invocations the
    # graph must finalise even though the LLM is still requesting tools.
    client = FakeLLMClient(
        handler=lambda _req: make_response(
            content="",
            tool_calls=(ToolCall(id="c", name="fs_read", arguments={}),),
            finish_reason="tool_call",
        )
    )
    deps = fake_deps(client, tool_registry=registry)
    graph = build_shell_agent_graph(deps)

    final = await graph.run(_state(user_prompt="hi", max_steps=2))

    assert final.finished is True
    output = final.data["output"]
    assert output["steps"] == 2  # type: ignore[index]
    assert output["truncated_by_max_steps"] is True  # type: ignore[index]
    failure = output["failure_explanation"]  # type: ignore[index]
    assert failure["category"] == "max_steps_truncated"
    # plan was called exactly max_steps times
    assert len(client.calls) == 2


async def test_output_usage_accumulates_across_multiple_plan_turns(tmp_path: Path) -> None:
    registry, _ = _registry_with("fs_read", content="hello")
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(ToolCall(id="c1", name="fs_read", arguments={"path": "x"}),),
                finish_reason="tool_call",
                usage=LLMUsage(prompt_tokens=3, completion_tokens=5, total_tokens=8),
            ),
            make_response(
                content="done",
                finish_reason="stop",
                usage=LLMUsage(prompt_tokens=7, completion_tokens=11, total_tokens=18),
            ),
        ]
    )
    graph = build_shell_agent_graph(fake_deps(client, tool_registry=registry))

    final = await graph.run(_state(user_prompt="hi", _workspace_path=str(tmp_path)))

    usage = final.data["output"]["usage"]  # type: ignore[index]
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 16
    assert usage["total_tokens"] == 26


async def test_max_total_tokens_cap_short_circuits_next_plan() -> None:
    registry, _ = _registry_with("fs_read")
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(ToolCall(id="c1", name="fs_read", arguments={}),),
                finish_reason="tool_call",
                usage=LLMUsage(prompt_tokens=3, completion_tokens=5, total_tokens=8),
            ),
            make_response(content="should-not-run", finish_reason="stop"),
        ]
    )
    graph = build_shell_agent_graph(fake_deps(client, tool_registry=registry))

    final = await graph.run(_state(user_prompt="hi", max_total_tokens=8))

    output = final.data["output"]
    assert output["steps"] == 1  # type: ignore[index]
    assert output["tool_invocations"] == 1  # type: ignore[index]
    assert output["truncated_by_token_budget"] is True  # type: ignore[index]
    failure = output["failure_explanation"]  # type: ignore[index]
    assert failure["category"] == "budget_exceeded"
    assert len(client.calls) == 1


# --------------------------------------------------------- permission gating


async def test_approve_each_tool_allow_decision_executes_tool_unchanged(
    tmp_path: Path,
) -> None:
    """When permission_mode=approve_each_tool, the gate is consulted; allow → execute."""

    import asyncio

    from meta_agent.core.domain.permission import PermissionDecision
    from meta_agent.infra.permission.in_memory import InMemoryPermissionGate

    registry, recorded = _registry_with("fs_read", content="payload")
    gate = InMemoryPermissionGate()

    async def auto_allow() -> None:
        # Watch for the prompt to register, then deliver an allow.
        for _ in range(50):
            await asyncio.sleep(0.01)
            if gate._pending:
                pending_id = next(iter(gate._pending))
                from datetime import UTC, datetime

                await gate.deliver(
                    PermissionDecision(
                        prompt_id=pending_id,
                        allow=True,
                        reason=None,
                        decided_at=datetime(2026, 6, 23, tzinfo=UTC),
                    )
                )
                return

    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(ToolCall(id="c1", name="fs_read", arguments={"path": "a"}),),
                finish_reason="tool_call",
            ),
            make_response(content="all done", finish_reason="stop"),
        ]
    )
    deps = fake_deps(client, tool_registry=registry, permission_gate=gate)
    graph = build_shell_agent_graph(deps)

    decider = asyncio.create_task(auto_allow())
    try:
        final = await graph.run(
            _state(
                user_prompt="hi",
                _workspace_path=str(tmp_path),
                _permission_mode="approve_each_tool",
            )
        )
    finally:
        await decider

    assert final.finished is True
    assert len(recorded) == 1
    assert recorded[0].name == "fs_read"
    # Second LLM call sees the actual tool result, not a denial.
    assert "payload" in client.calls[1].messages[-1].content


async def test_approve_each_tool_deny_decision_skips_executor(tmp_path: Path) -> None:
    """A deny decision short-circuits the tool — executor never invoked."""

    import asyncio

    from meta_agent.core.domain.permission import PermissionDecision
    from meta_agent.infra.permission.in_memory import InMemoryPermissionGate

    registry, recorded = _registry_with("fs_read", content="payload")
    gate = InMemoryPermissionGate()

    async def auto_deny() -> None:
        for _ in range(50):
            await asyncio.sleep(0.01)
            if gate._pending:
                pending_id = next(iter(gate._pending))
                from datetime import UTC, datetime

                await gate.deliver(
                    PermissionDecision(
                        prompt_id=pending_id,
                        allow=False,
                        reason="too risky",
                        decided_at=datetime(2026, 6, 23, tzinfo=UTC),
                    )
                )
                return

    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(ToolCall(id="c1", name="fs_read", arguments={"path": "a"}),),
                finish_reason="tool_call",
            ),
            make_response(content="okay I won't", finish_reason="stop"),
        ]
    )
    deps = fake_deps(client, tool_registry=registry, permission_gate=gate)
    graph = build_shell_agent_graph(deps)

    decider = asyncio.create_task(auto_deny())
    try:
        final = await graph.run(
            _state(
                user_prompt="hi",
                _workspace_path=str(tmp_path),
                _permission_mode="approve_each_tool",
            )
        )
    finally:
        await decider

    assert final.finished is True
    assert recorded == []  # tool handler never ran
    # The LLM saw a synthetic tool result carrying the denial.
    second_call = client.calls[1]
    tool_msg = second_call.messages[-1]
    assert "permission_denied" in tool_msg.content
    assert "too risky" in tool_msg.content


async def test_auto_mode_bypasses_gate_entirely(tmp_path: Path) -> None:
    """PermissionMode.AUTO short-circuits the gate even when one is configured."""

    from meta_agent.infra.permission.in_memory import InMemoryPermissionGate

    registry, recorded = _registry_with("fs_read", content="payload")
    gate = InMemoryPermissionGate()  # never called

    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(ToolCall(id="c1", name="fs_read", arguments={"path": "a"}),),
                finish_reason="tool_call",
            ),
            make_response(content="all done", finish_reason="stop"),
        ]
    )
    deps = fake_deps(client, tool_registry=registry, permission_gate=gate)
    graph = build_shell_agent_graph(deps)

    final = await graph.run(
        _state(
            user_prompt="hi",
            _workspace_path=str(tmp_path),
            _permission_mode="auto",
        )
    )
    assert final.finished is True
    assert len(recorded) == 1
    assert gate._pending == {}  # gate was never consulted


# --------------------------------------------------- multi-turn context


async def test_prior_messages_are_prepended_before_user_prompt() -> None:
    """``_prior_messages`` from a session land in the first LLM call's messages."""

    client = FakeLLMClient(response=make_response(content="final", model="fake/m"))
    registry, _ = _registry_with("noop")
    deps = fake_deps(client, tool_registry=registry)
    graph = build_shell_agent_graph(deps)

    await graph.run(
        _state(
            user_prompt="continue please",
            _prior_messages=[
                {"role": "user", "content": "do the thing"},
                {"role": "assistant", "content": "I did it"},
            ],
        )
    )
    first_call = client.calls[0]
    # System prompt may or may not be present depending on defaults;
    # filter to user/assistant + ordered.
    roles_contents = [(m.role.value, m.content) for m in first_call.messages]
    # Expected slice: prior user + prior assistant + current user.
    assert ("user", "do the thing") in roles_contents
    assert ("assistant", "I did it") in roles_contents
    # Current prompt is the last message.
    assert roles_contents[-1] == ("user", "continue please")
    # Prior pair must come before the current user prompt.
    prior_user_idx = roles_contents.index(("user", "do the thing"))
    prior_asst_idx = roles_contents.index(("assistant", "I did it"))
    current_user_idx = len(roles_contents) - 1
    assert prior_user_idx < prior_asst_idx < current_user_idx


async def test_missing_prior_messages_key_behaves_as_single_shot() -> None:
    """A task without ``_prior_messages`` builds the initial conversation as before."""

    client = FakeLLMClient(response=make_response(content="final"))
    registry, _ = _registry_with("noop")
    deps = fake_deps(client, tool_registry=registry)
    graph = build_shell_agent_graph(deps)
    await graph.run(_state(user_prompt="hi"))
    contents = [m.content for m in client.calls[0].messages]
    assert contents[-1] == "hi"
    # No prior user/assistant pair landed.
    assert "do the thing" not in contents


async def test_malformed_prior_messages_raises_graph_error() -> None:
    """A non-list / bad-role entry surfaces a clear GraphError."""

    from meta_agent.core.orchestration.graph import GraphError

    client = FakeLLMClient(response=make_response(content="final"))
    registry, _ = _registry_with("noop")
    deps = fake_deps(client, tool_registry=registry)
    graph = build_shell_agent_graph(deps)

    with pytest.raises(GraphError, match="_prior_messages"):
        await graph.run(
            _state(
                user_prompt="hi",
                _prior_messages="not a list",
            )
        )


# ----------------------------------------------------- plan mode


async def test_plan_mode_allow_executes_all_pending_tool_calls(tmp_path: Path) -> None:
    """One plan-level approval green-lights every tool call in the batch."""

    import asyncio
    from datetime import UTC, datetime

    from meta_agent.core.domain.permission import PermissionDecision
    from meta_agent.infra.permission.in_memory import InMemoryPermissionGate

    registry, recorded = _registry_with("fs_read", content="payload")
    gate = InMemoryPermissionGate()

    async def auto_allow() -> None:
        for _ in range(80):
            await asyncio.sleep(0.01)
            if gate._pending:
                pending_id = next(iter(gate._pending))
                await gate.deliver(
                    PermissionDecision(
                        prompt_id=pending_id,
                        allow=True,
                        reason=None,
                        decided_at=datetime(2026, 6, 23, tzinfo=UTC),
                    )
                )
                return

    client = FakeLLMClient(
        responses=[
            # First planning step proposes two tool calls.
            make_response(
                content="I will read both files then summarize.",
                tool_calls=(
                    ToolCall(id="c1", name="fs_read", arguments={"path": "a"}),
                    ToolCall(id="c2", name="fs_read", arguments={"path": "b"}),
                ),
                finish_reason="tool_call",
            ),
            make_response(content="done", finish_reason="stop"),
        ]
    )
    deps = fake_deps(client, tool_registry=registry, permission_gate=gate)
    graph = build_shell_agent_graph(deps)

    decider = asyncio.create_task(auto_allow())
    try:
        final = await graph.run(
            _state(
                user_prompt="please",
                _workspace_path=str(tmp_path),
                _permission_mode="plan",
            )
        )
    finally:
        await decider

    assert final.finished is True
    # Both tools ran on a single allow.
    assert [c.name for c in recorded] == ["fs_read", "fs_read"]
    assert [c.arguments["path"] for c in recorded] == ["a", "b"]
    # Only ONE prompt was issued for the batch (the gate's _pending
    # dict was empty once delivered + drained).
    assert gate._pending == {}


async def test_plan_mode_deny_skips_all_tool_calls_and_feeds_reason(
    tmp_path: Path,
) -> None:
    """A deny short-circuits the whole batch; reason flows into the model's view."""

    import asyncio
    from datetime import UTC, datetime

    from meta_agent.core.domain.permission import PermissionDecision
    from meta_agent.infra.permission.in_memory import InMemoryPermissionGate

    registry, recorded = _registry_with("fs_read", content="payload")
    gate = InMemoryPermissionGate()

    async def auto_deny() -> None:
        for _ in range(80):
            await asyncio.sleep(0.01)
            if gate._pending:
                pending_id = next(iter(gate._pending))
                await gate.deliver(
                    PermissionDecision(
                        prompt_id=pending_id,
                        allow=False,
                        reason="that plan is too risky",
                        decided_at=datetime(2026, 6, 23, tzinfo=UTC),
                    )
                )
                return

    client = FakeLLMClient(
        responses=[
            make_response(
                content="My plan: do A then B.",
                tool_calls=(
                    ToolCall(id="c1", name="fs_read", arguments={"path": "a"}),
                    ToolCall(id="c2", name="fs_read", arguments={"path": "b"}),
                ),
                finish_reason="tool_call",
            ),
            make_response(content="OK I won't", finish_reason="stop"),
        ]
    )
    deps = fake_deps(client, tool_registry=registry, permission_gate=gate)
    graph = build_shell_agent_graph(deps)

    decider = asyncio.create_task(auto_deny())
    try:
        final = await graph.run(
            _state(
                user_prompt="please",
                _workspace_path=str(tmp_path),
                _permission_mode="plan",
            )
        )
    finally:
        await decider

    assert final.finished is True
    # No tool actually ran.
    assert recorded == []
    # Both tool slots received a synthetic deny message that the
    # model saw on the second LLM call.
    second_call = client.calls[1]
    tool_messages = [m for m in second_call.messages if m.role.value == "tool"]
    assert len(tool_messages) == 2
    for msg in tool_messages:
        assert "plan_denied" in msg.content
        assert "too risky" in msg.content


async def test_plan_mode_with_no_gate_falls_back_to_auto_execution(
    tmp_path: Path,
) -> None:
    """When ``deps.permission_gate is None``, plan mode behaves like auto."""

    registry, recorded = _registry_with("fs_read", content="payload")
    client = FakeLLMClient(
        responses=[
            make_response(
                content="",
                tool_calls=(ToolCall(id="c1", name="fs_read", arguments={"path": "a"}),),
                finish_reason="tool_call",
            ),
            make_response(content="done", finish_reason="stop"),
        ]
    )
    deps = fake_deps(client, tool_registry=registry, permission_gate=None)
    graph = build_shell_agent_graph(deps)
    final = await graph.run(
        _state(
            user_prompt="hi",
            _workspace_path=str(tmp_path),
            _permission_mode="plan",
        )
    )
    assert final.finished is True
    assert len(recorded) == 1
