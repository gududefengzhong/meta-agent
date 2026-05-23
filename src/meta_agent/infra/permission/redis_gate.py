"""Redis pub/sub :class:`PermissionGate` backend.

Wire format
===========
* Decisions are published as JSON on channel
  ``permission:decision:{prompt_id}``.
* :meth:`request` subscribes to that channel before announcing the
  prompt — subscriber-then-publisher ordering matters because Redis
  pub/sub has no replay; a decision delivered before the subscribe
  call lands would be lost.
* :meth:`request` also publishes the prompt itself on
  ``permission:prompt:{tenant_id}:{task_id}`` so an SSE consumer
  can surface it to the connected client. The prompt channel is
  fire-and-forget — the gate itself does not subscribe to it; that
  is the API tier's job.

Connection lifecycle
====================
The gate does not own the :class:`Redis` client. Construct it
externally so the queue / rate-limiter / breaker / broadcaster /
gate all share one connection pool.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from meta_agent.core.domain.permission import PermissionDecision, PermissionPrompt
from meta_agent.core.ports.permission_gate import (
    PermissionGate,
    PermissionGateError,
    PermissionTimeoutError,
)

logger = logging.getLogger(__name__)

_DEFAULT_PROMPT_PREFIX = "permission:prompt"
_DEFAULT_DECISION_PREFIX = "permission:decision"


class RedisPermissionGate(PermissionGate):
    """Pub/sub-backed prompt → decision round-trip across worker + API."""

    def __init__(
        self,
        client: Redis,
        *,
        prompt_channel_prefix: str = _DEFAULT_PROMPT_PREFIX,
        decision_channel_prefix: str = _DEFAULT_DECISION_PREFIX,
    ) -> None:
        if not prompt_channel_prefix or not decision_channel_prefix:
            raise ValueError("channel prefixes must be non-empty")
        self._client = client
        self._prompt_prefix = prompt_channel_prefix
        self._decision_prefix = decision_channel_prefix

    async def request(
        self,
        prompt: PermissionPrompt,
        *,
        timeout_seconds: float,
    ) -> PermissionDecision:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        decision_channel = self._decision_channel(prompt.prompt_id)
        pubsub: Any = self._client.pubsub()
        try:
            try:
                await pubsub.subscribe(decision_channel)
            except RedisError as exc:
                raise PermissionGateError(
                    f"redis subscribe failed for {decision_channel!r}"
                ) from exc
            # Publish the prompt only after the subscription is live
            # so a fast decision can't slip past us.
            prompt_channel = self._prompt_channel(prompt.tenant_id, prompt.task_id)
            try:
                await self._client.publish(prompt_channel, prompt.model_dump_json())
            except RedisError as exc:
                raise PermissionGateError(f"redis publish failed for {prompt_channel!r}") from exc
            try:
                return await asyncio.wait_for(
                    self._wait_for_decision(pubsub, decision_channel),
                    timeout=timeout_seconds,
                )
            except TimeoutError as exc:
                raise PermissionTimeoutError(
                    f"no decision for prompt_id={prompt.prompt_id!r} within {timeout_seconds}s"
                ) from exc
        finally:
            await _safe_close_pubsub(pubsub)

    async def deliver(self, decision: PermissionDecision) -> None:
        channel = self._decision_channel(decision.prompt_id)
        try:
            await self._client.publish(channel, decision.model_dump_json())
        except RedisError as exc:
            raise PermissionGateError(f"redis publish failed for {channel!r}") from exc

    async def close(self) -> None:
        # The shared client is owned by the lifespan that built it.
        return None

    async def _wait_for_decision(self, pubsub: Any, channel: str) -> PermissionDecision:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            data = message.get("data")
            if isinstance(data, bytes):
                try:
                    payload = data.decode("utf-8")
                except UnicodeDecodeError:
                    logger.warning(
                        "permission.gate.invalid_utf8",
                        extra={"channel": channel, "bytes": len(data)},
                    )
                    continue
            elif isinstance(data, str):
                payload = data
            else:
                logger.warning(
                    "permission.gate.unexpected_data_type",
                    extra={"channel": channel, "type": type(data).__name__},
                )
                continue
            try:
                return PermissionDecision.model_validate_json(payload)
            except ValidationError as exc:
                logger.warning(
                    "permission.gate.invalid_decision",
                    extra={"channel": channel, "error": str(exc)[:200]},
                )
                continue
        raise PermissionGateError(f"redis pubsub closed before delivering decision on {channel!r}")

    def _prompt_channel(self, tenant_id: str, task_id: str) -> str:
        return f"{self._prompt_prefix}:{tenant_id}:{task_id}"

    def _decision_channel(self, prompt_id: str) -> str:
        return f"{self._decision_prefix}:{prompt_id}"


async def _safe_close_pubsub(pubsub: Any) -> None:
    """Best-effort pubsub teardown that swallows shutdown errors."""
    try:
        await pubsub.unsubscribe()
    except RedisError as exc:
        logger.debug(
            "permission.gate.unsubscribe_error",
            extra={"error_type": type(exc).__name__},
        )
    try:
        await pubsub.aclose()
    except (RedisError, AttributeError) as exc:
        logger.debug(
            "permission.gate.pubsub_close_error",
            extra={"error_type": type(exc).__name__},
        )
