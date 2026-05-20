"""TokenValidator adapters.

【目标】解析 ``Authorization: Bearer`` 时把 token 换成
:class:`Principal`。env / DB 双实现满足 α 阶段。

【当前】``EnvTokenValidator``（CSV from env）+ ``PgTokenValidator``
（``api_keys`` 表 + sha256 哈希）+ env-driven factory。
"""

from meta_agent.infra.auth.config import (
    AuthBackend,
    AuthConfig,
    build_token_validator_from_config,
)
from meta_agent.infra.auth.env_validator import EnvTokenValidator
from meta_agent.infra.auth.pg_validator import PgTokenValidator

__all__ = [
    "AuthBackend",
    "AuthConfig",
    "EnvTokenValidator",
    "PgTokenValidator",
    "build_token_validator_from_config",
]
