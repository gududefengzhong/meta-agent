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
