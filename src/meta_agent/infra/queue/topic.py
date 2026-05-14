"""Topic-to-stream name mapping.

Centralised here so a future migration to a different broker can swap
the convention in one place. The current rule is intentionally trivial:
``topic`` is treated as the stream key, optionally prefixed for
namespacing if the deployment shares a Redis instance.
"""

from __future__ import annotations

DEFAULT_STREAM_PREFIX = "meta_agent.stream."


def stream_name_for_topic(topic: str, *, prefix: str = DEFAULT_STREAM_PREFIX) -> str:
    """Return the Redis stream key for the given logical ``topic``."""
    if not topic:
        raise ValueError("topic must be non-empty")
    return f"{prefix}{topic}"
