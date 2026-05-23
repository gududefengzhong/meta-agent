"""Cross-process streaming infra (Phase δ-1).

Adapters for the :class:`ChunkBroadcaster` port. The in-memory
backend is used by unit tests + single-process dev mode; the Redis
backend is the production default.
"""

from meta_agent.infra.streaming.in_memory import InMemoryChunkBroadcaster
from meta_agent.infra.streaming.redis_broadcaster import RedisChunkBroadcaster

__all__ = [
    "InMemoryChunkBroadcaster",
    "RedisChunkBroadcaster",
]
