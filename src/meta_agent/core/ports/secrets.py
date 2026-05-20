"""Secrets-resolution port.

【目标】把 ``OPENROUTER_API_KEY`` / ``META_AGENT_GITHUB_TOKEN`` 这类
凭据的来源从 ``os.environ`` 显式收口。env / 文件双实现满足 α 阶段；
KMS / Vault 留 Port 占位。

【设计取舍】

* ``get`` 是 async，留出向 KMS / Vault 这类网络后端扩展的余地；
  env / 文件实现里仍然是常数时间。
* 用字符串键（``"openrouter.api_key"`` / ``"github.token"``）而非
  枚举：键是稀疏命名空间，新增 key 不应改动 Port 模块。常量集中在
  :mod:`meta_agent.core.ports.secrets` 顶层供调用方引用。
* "not found" 与 "backend fault" 走两个不同异常：上层据此决定
  fail-open / -closed，而不是去解析 message 字符串。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Final

# ── Well-known secret keys ────────────────────────────────────────────────────
#
# 每个键代表一类外部凭据。``infra/secrets`` 适配层把这些键映射到
# 具体后端（环境变量、文件 JSON 字段、KMS path 等）。

SECRET_KEY_OPENROUTER_API_KEY: Final[str] = "openrouter.api_key"
SECRET_KEY_GITHUB_TOKEN: Final[str] = "github.token"

KNOWN_SECRET_KEYS: Final[tuple[str, ...]] = (
    SECRET_KEY_OPENROUTER_API_KEY,
    SECRET_KEY_GITHUB_TOKEN,
)


class SecretNotFoundError(KeyError):
    """Raised when a backend has no value for the requested key."""


class SecretBackendError(Exception):
    """Raised when the secret backend itself failed (file unreadable, KMS down)."""


class Secrets(ABC):
    """Lookup-by-key access to deployment secrets."""

    @abstractmethod
    async def get(self, key: str) -> str:
        """Return the secret value for ``key``.

        Implementations MUST raise :class:`SecretNotFoundError` for an
        unknown / unset key and :class:`SecretBackendError` for any
        genuine backend fault. They MUST NOT return empty strings to
        signal absence — callers should be able to distinguish "no
        configured value" from "configured to empty".
        """


__all__ = [
    "KNOWN_SECRET_KEYS",
    "SECRET_KEY_GITHUB_TOKEN",
    "SECRET_KEY_OPENROUTER_API_KEY",
    "SecretBackendError",
    "SecretNotFoundError",
    "Secrets",
]
