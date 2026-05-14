"""Security and identity layer.

【目标】RequestContext、Secret 接入、RBAC、SSO/OIDC、输入防护。
【当前】仅 RequestContext 与上下文传播原语。
"""

from meta_agent.infra.security.context import (
    MissingContextError,
    RequestContext,
    bind_context,
    get_current,
    require_current,
    require_tenant_id,
    update_context,
)

__all__ = [
    "MissingContextError",
    "RequestContext",
    "bind_context",
    "get_current",
    "require_current",
    "require_tenant_id",
    "update_context",
]
