"""Inline-permission backends (Phase δ-1)."""

from meta_agent.infra.permission.in_memory import InMemoryPermissionGate
from meta_agent.infra.permission.redis_gate import RedisPermissionGate

__all__ = ["InMemoryPermissionGate", "RedisPermissionGate"]
