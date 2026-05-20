"""Token-based ingress authentication port.

【目标】解析 ``Authorization: Bearer <token>`` 时把 token 换成被信任的
``Principal``；SSO / OIDC 留 Port，不实现。

【约定】``validate`` 返回 ``Principal | None`` 是正常控制流：``None``
表示 token 不存在 / 已撤销 / 不匹配。只有真正的后端故障（DB 不通、
配置损坏）才抛 :class:`AuthBackendError`，让中间件统一决定 5xx 而
不是 401。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Principal:
    """A validated caller identity.

    ``tenant_id`` 和 ``principal_id`` 必须由验证后端（env / DB）填，
    而非来自请求头 —— 否则任意客户端都能伪造租户。``scopes`` 保留
    供未来 RBAC 闸门使用；当下不查。
    """

    tenant_id: str
    principal_id: str
    scopes: tuple[str, ...] = field(default_factory=tuple)


class AuthBackendError(Exception):
    """Raised when a token store / verifier backend fails.

    Distinguishes infrastructure faults from token-not-found so the
    middleware can map the former to 5xx and the latter to 401.
    """


class TokenValidator(ABC):
    """Validate an opaque bearer token, return the caller's :class:`Principal`."""

    @abstractmethod
    async def validate(self, token: str) -> Principal | None:
        """Return the principal bound to ``token``, or ``None`` if unknown.

        Implementations MUST use a constant-time comparison when matching
        tokens against stored material to avoid timing oracles. Returning
        ``None`` covers: empty token, malformed token, unknown token,
        revoked token. Backend faults raise :class:`AuthBackendError`.
        """


__all__ = ["AuthBackendError", "Principal", "TokenValidator"]
