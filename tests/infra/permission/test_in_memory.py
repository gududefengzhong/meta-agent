"""Unit tests for :class:`InMemoryPermissionGate`.

Covers the rendezvous contract:

* publish-then-decide round-trip returns the decision to the awaiter
* timeout raises :class:`PermissionTimeoutError` and cleans up the
  pending future
* deliver for an unknown / already-resolved prompt is a no-op
  (matches the port contract)
* concurrent awaiters can't share a ``prompt_id``
* ``close`` cancels outstanding awaiters cleanly
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from meta_agent.core.domain.permission import PermissionDecision, PermissionPrompt
from meta_agent.core.ports.permission_gate import PermissionTimeoutError
from meta_agent.infra.permission.in_memory import InMemoryPermissionGate


def _prompt(prompt_id: str = "prm-1") -> PermissionPrompt:
    return PermissionPrompt(
        prompt_id=prompt_id,
        tenant_id="t-1",
        task_id="task-1",
        tool_name="shell",
        summary="run shell command",
        payload={"cmd": "ls"},
        created_at=datetime(2026, 6, 23, tzinfo=UTC),
    )


def _decision(prompt_id: str = "prm-1", *, allow: bool = True) -> PermissionDecision:
    return PermissionDecision(
        prompt_id=prompt_id,
        allow=allow,
        reason=None if allow else "no",
        decided_at=datetime(2026, 6, 23, tzinfo=UTC),
    )


async def test_request_returns_delivered_decision() -> None:
    gate = InMemoryPermissionGate()

    async def deliver_after_delay() -> None:
        await asyncio.sleep(0.01)
        await gate.deliver(_decision(allow=True))

    deliver = asyncio.create_task(deliver_after_delay())
    try:
        decision = await gate.request(_prompt(), timeout_seconds=1.0)
    finally:
        await deliver
    assert decision.allow is True
    assert decision.prompt_id == "prm-1"


async def test_request_times_out_when_no_decision_arrives() -> None:
    gate = InMemoryPermissionGate()
    with pytest.raises(PermissionTimeoutError):
        await gate.request(_prompt(), timeout_seconds=0.05)


async def test_timeout_unregisters_pending_future() -> None:
    gate = InMemoryPermissionGate()
    with pytest.raises(PermissionTimeoutError):
        await gate.request(_prompt(), timeout_seconds=0.05)
    # A second request for the same prompt_id MUST succeed — the
    # timeout cleanup means the registration is fresh.
    deliver = asyncio.create_task(_deliver_after(gate, _decision(), 0.01))
    try:
        decision = await gate.request(_prompt(), timeout_seconds=1.0)
    finally:
        await deliver
    assert decision.allow is True


async def test_deliver_for_unknown_prompt_is_noop() -> None:
    gate = InMemoryPermissionGate()
    # No request ever made — deliver must not raise.
    await gate.deliver(_decision("never-requested"))


async def test_deliver_after_completion_is_noop() -> None:
    gate = InMemoryPermissionGate()
    deliver = asyncio.create_task(_deliver_after(gate, _decision(allow=True), 0.01))
    try:
        await gate.request(_prompt(), timeout_seconds=1.0)
    finally:
        await deliver
    # Now the future is resolved + removed. A second deliver must
    # be silently swallowed.
    await gate.deliver(_decision(allow=False))


async def test_duplicate_prompt_id_rejected() -> None:
    gate = InMemoryPermissionGate()
    pending = asyncio.create_task(gate.request(_prompt(), timeout_seconds=0.5))
    await asyncio.sleep(0.01)  # let the first registration land
    with pytest.raises(ValueError, match="already has a pending request"):
        await gate.request(_prompt(), timeout_seconds=0.5)
    await gate.deliver(_decision())
    await pending  # drain so the test doesn't leave a dangling task


async def test_zero_timeout_rejected() -> None:
    gate = InMemoryPermissionGate()
    with pytest.raises(ValueError, match="timeout_seconds must be > 0"):
        await gate.request(_prompt(), timeout_seconds=0.0)


async def test_close_cancels_outstanding_awaiters() -> None:
    gate = InMemoryPermissionGate()
    pending = asyncio.create_task(gate.request(_prompt(), timeout_seconds=5.0))
    await asyncio.sleep(0.01)
    await gate.close()
    with pytest.raises(asyncio.CancelledError):
        await pending


async def _deliver_after(
    gate: InMemoryPermissionGate, decision: PermissionDecision, delay: float
) -> None:
    await asyncio.sleep(delay)
    await gate.deliver(decision)
