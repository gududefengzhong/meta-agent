"""End-to-end :class:`RedisPermissionGate` against a real Redis.

Verifies the worker↔API rendezvous works through the actual Redis
pub/sub pipeline that production uses. Two simulated processes
share one :class:`Redis` client — sufficient to prove the wire
encoding round-trips even though in production they'd be separate
:class:`Redis` instances connected to the same server.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis

from meta_agent.core.domain.permission import PermissionDecision, PermissionPrompt
from meta_agent.core.ports.permission_gate import PermissionTimeoutError
from meta_agent.infra.permission.redis_gate import RedisPermissionGate


def _prompt(prompt_id: str = "prm-it-1") -> PermissionPrompt:
    return PermissionPrompt(
        prompt_id=prompt_id,
        tenant_id="t-1",
        task_id="task-1",
        tool_name="shell",
        summary="run shell command",
        payload={"cmd": "ls -la"},
        created_at=datetime(2026, 6, 23, tzinfo=UTC),
    )


def _decision(prompt_id: str = "prm-it-1", *, allow: bool = True) -> PermissionDecision:
    return PermissionDecision(
        prompt_id=prompt_id,
        allow=allow,
        reason="looks fine" if allow else "no thanks",
        decided_at=datetime(2026, 6, 23, tzinfo=UTC),
    )


async def test_request_then_deliver_round_trips_through_redis(
    redis_client: Redis,
) -> None:
    gate = RedisPermissionGate(redis_client)

    async def deliver_after_delay() -> None:
        # Give the worker side a moment to subscribe before we
        # publish the decision; pub/sub has no replay.
        await asyncio.sleep(0.1)
        await gate.deliver(_decision(allow=True))

    deliver_task = asyncio.create_task(deliver_after_delay())
    try:
        decision = await asyncio.wait_for(gate.request(_prompt(), timeout_seconds=3.0), timeout=5.0)
    finally:
        await deliver_task
        await gate.close()

    assert decision.allow is True
    assert decision.prompt_id == "prm-it-1"
    assert decision.reason == "looks fine"


async def test_request_times_out_when_no_decision_arrives(
    redis_client: Redis,
) -> None:
    gate = RedisPermissionGate(redis_client)
    try:
        with pytest.raises(PermissionTimeoutError):
            await gate.request(_prompt("prm-timeout"), timeout_seconds=0.2)
    finally:
        await gate.close()


async def test_deny_decision_is_routed_with_reason(redis_client: Redis) -> None:
    gate = RedisPermissionGate(redis_client)

    async def deny_after_delay() -> None:
        await asyncio.sleep(0.1)
        await gate.deliver(_decision("prm-deny", allow=False))

    deliver_task = asyncio.create_task(deny_after_delay())
    try:
        decision = await asyncio.wait_for(
            gate.request(_prompt("prm-deny"), timeout_seconds=3.0), timeout=5.0
        )
    finally:
        await deliver_task
        await gate.close()

    assert decision.allow is False
    assert decision.reason == "no thanks"


async def test_subscribe_prompts_receives_published_prompt(redis_client: Redis) -> None:
    """End-to-end: API-side subscriber sees prompts fanned out by ``request``."""

    gate = RedisPermissionGate(redis_client)
    sub = await gate.subscribe_prompts(tenant_id="t-1", task_id="task-1")

    async def issue_prompt_then_resolve() -> None:
        # Give the subscriber a moment to register before we publish.
        await asyncio.sleep(0.1)
        await gate.deliver(_decision("prm-sub-1", allow=True))

    deliver_task = asyncio.create_task(issue_prompt_then_resolve())
    requester = asyncio.create_task(gate.request(_prompt("prm-sub-1"), timeout_seconds=3.0))
    try:
        received = await asyncio.wait_for(sub.__anext__(), timeout=3.0)
        await requester
    finally:
        await deliver_task
        await sub.aclose()
        await gate.close()

    assert received.prompt_id == "prm-sub-1"
    assert received.tenant_id == "t-1"
    assert received.task_id == "task-1"


async def test_subscribe_prompts_tenant_isolated_through_redis(
    redis_client: Redis,
) -> None:
    gate = RedisPermissionGate(redis_client)
    sub_a = await gate.subscribe_prompts(tenant_id="t-A", task_id="task-1")
    sub_b = await gate.subscribe_prompts(tenant_id="t-B", task_id="task-1")

    a_prompt = PermissionPrompt(
        prompt_id="prm-iso-a",
        tenant_id="t-A",
        task_id="task-1",
        tool_name="shell",
        summary="A only",
        payload={},
        created_at=datetime(2026, 6, 23, tzinfo=UTC),
    )

    async def deliver_after_delay() -> None:
        await asyncio.sleep(0.2)
        await gate.deliver(_decision("prm-iso-a"))

    deliver_task = asyncio.create_task(deliver_after_delay())
    requester = asyncio.create_task(gate.request(a_prompt, timeout_seconds=3.0))
    try:
        a_received = await asyncio.wait_for(sub_a.__anext__(), timeout=3.0)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(sub_b.__anext__(), timeout=0.3)
        await requester
    finally:
        await deliver_task
        await sub_a.aclose()
        await sub_b.aclose()
        await gate.close()

    assert a_received.prompt_id == "prm-iso-a"
