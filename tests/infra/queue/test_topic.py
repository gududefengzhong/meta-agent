"""Unit tests for topic → Redis stream key mapping."""

from __future__ import annotations

import pytest

from meta_agent.infra.queue.topic import (
    DEFAULT_STREAM_PREFIX,
    stream_name_for_topic,
)


def test_stream_name_uses_default_prefix() -> None:
    assert stream_name_for_topic("task.events") == f"{DEFAULT_STREAM_PREFIX}task.events"


def test_stream_name_accepts_custom_prefix() -> None:
    assert stream_name_for_topic("task.events", prefix="ns:") == "ns:task.events"


def test_stream_name_rejects_empty_topic() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        stream_name_for_topic("")
