"""Secrets adapters.

【目标】把 ``OPENROUTER_API_KEY`` / ``META_AGENT_GITHUB_TOKEN`` 这类
凭据来源从 ``os.environ`` 收口。env 默认实现 + 文件实现满足 α 阶段；
KMS / Vault 留 Port 占位。

【当前】``EnvSecrets`` + ``FileSecrets`` + env-driven factory +
``resolve_secret_env`` 辅助：把已知 secret key 折叠回 env dict 喂给
现有 ``from_env`` 构造路径。
"""

from meta_agent.infra.secrets.config import (
    SecretsBackend,
    SecretsConfig,
    build_secrets_from_config,
    build_secrets_from_env,
)
from meta_agent.infra.secrets.env import EnvSecrets
from meta_agent.infra.secrets.file import FileSecrets
from meta_agent.infra.secrets.resolver import (
    SECRET_TO_ENV_NAME,
    resolve_secret_env,
)

__all__ = [
    "SECRET_TO_ENV_NAME",
    "EnvSecrets",
    "FileSecrets",
    "SecretsBackend",
    "SecretsConfig",
    "build_secrets_from_config",
    "build_secrets_from_env",
    "resolve_secret_env",
]
