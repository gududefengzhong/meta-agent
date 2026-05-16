"""Unit tests for the worker process bootstrap.

These tests exercise pure wiring helpers (settings parsing and
registry assembly). The real :func:`build_worker` opens Postgres /
Redis / OpenRouter connections; that path is intentionally covered by
the integration suite (``tests/integration``) and the ``docker
compose`` smoke flow rather than by mocking every adapter here.
"""

from __future__ import annotations

import pytest

from meta_agent.core.domain.task import TaskType
from meta_agent.core.orchestration import GraphDeps
from meta_agent.core.orchestration.graphs import ECHO_GRAPH_ID, SIMPLE_CHAT_GRAPH_ID
from meta_agent.worker.bootstrap import (
    WorkerSettings,
    build_registry,
)
from tests.core.orchestration._fakes import FakeLLMClient


def _env(**overrides: str) -> dict[str, str]:
    base: dict[str, str] = {
        "OPENROUTER_API_KEY": "sk-or-test-1234",
    }
    base.update(overrides)
    return base


def test_settings_from_env_uses_documented_defaults() -> None:
    settings = WorkerSettings.from_env(_env())
    assert settings.db_url.startswith("postgresql://")
    assert settings.redis_url.startswith("redis://")
    assert settings.task_topic == "task.commands"
    assert settings.consumer_group == "workers"
    assert settings.consumer_name  # hostname-derived, non-empty
    assert settings.max_attempts == 3
    assert settings.block_ms == 1_000
    assert settings.openrouter.api_key == "sk-or-test-1234"


def test_settings_from_env_overrides_each_knob() -> None:
    settings = WorkerSettings.from_env(
        _env(
            META_AGENT_DB_URL="postgresql://u:p@db:5432/x",
            META_AGENT_REDIS_URL="redis://r:6379/2",
            META_AGENT_TASK_TOPIC="custom.topic",
            META_AGENT_WORKER_GROUP="g-1",
            META_AGENT_WORKER_NAME="worker-7",
            META_AGENT_WORKER_MAX_ATTEMPTS="5",
            META_AGENT_WORKER_BLOCK_MS="250",
            META_AGENT_WORKER_DB_MIN_SIZE="2",
            META_AGENT_WORKER_DB_MAX_SIZE="20",
        )
    )
    assert settings.db_url == "postgresql://u:p@db:5432/x"
    assert settings.redis_url == "redis://r:6379/2"
    assert settings.task_topic == "custom.topic"
    assert settings.consumer_group == "g-1"
    assert settings.consumer_name == "worker-7"
    assert settings.max_attempts == 5
    assert settings.block_ms == 250
    assert settings.db_min_size == 2
    assert settings.db_max_size == 20


def test_settings_from_env_requires_openrouter_key() -> None:
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        WorkerSettings.from_env({})


def test_build_registry_registers_builtin_graphs_and_routes_defaults() -> None:
    registry = build_registry(GraphDeps(llm=FakeLLMClient()))
    assert registry.is_materialized
    assert registry.get(ECHO_GRAPH_ID).graph_id == ECHO_GRAPH_ID
    assert registry.get(SIMPLE_CHAT_GRAPH_ID).graph_id == SIMPLE_CHAT_GRAPH_ID
    assert registry.resolve(TaskType.SYSTEM_ECHO).graph_id == ECHO_GRAPH_ID
    assert registry.resolve(TaskType.SYSTEM_CHAT).graph_id == SIMPLE_CHAT_GRAPH_ID
