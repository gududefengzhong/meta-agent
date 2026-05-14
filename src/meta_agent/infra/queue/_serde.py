"""(De)serialization of :class:`MessageEnvelope` for Redis Streams.

Redis Streams entries are flat ``{field: value}`` maps of bytes; the
adapter stores the entire envelope as a single ``payload`` field
holding the JSON representation. This keeps the schema versioning
problem inside Pydantic instead of leaking into Redis field naming.
"""

from __future__ import annotations

from meta_agent.core.ports.message import MessageEnvelope

_PAYLOAD_FIELD = "envelope"

# redis-py xadd accepts a wide key/value union; widen here so callers
# do not need their own ``cast`` at the call site.
_StreamPrimitive = bytes | bytearray | memoryview | str | int | float
StreamFields = dict[_StreamPrimitive, _StreamPrimitive]


def envelope_to_fields(envelope: MessageEnvelope) -> StreamFields:
    """Encode ``envelope`` into a Redis-stream field map."""
    return {_PAYLOAD_FIELD: envelope.model_dump_json()}


def fields_to_envelope(fields: dict[bytes | str, bytes | str]) -> MessageEnvelope:
    """Decode a Redis-stream field map back into an envelope.

    Accepts both ``bytes`` and ``str`` keys/values to tolerate the
    redis-py default of returning bytes when ``decode_responses=False``.
    """
    raw: str | bytes | None = None
    for key, value in fields.items():
        key_str = key.decode() if isinstance(key, bytes) else key
        if key_str == _PAYLOAD_FIELD:
            raw = value
            break
    if raw is None:
        raise ValueError(f"Redis stream entry missing {_PAYLOAD_FIELD!r} field")
    text = raw.decode() if isinstance(raw, bytes) else raw
    return MessageEnvelope.model_validate_json(text)
