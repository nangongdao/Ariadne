"""JWT Web Console 会话。

M6 §6 选择 pyjwt：成熟实现，手写签名验证极易出错。

API Key 用于 SDK/程序化访问；JWT 用于浏览器会话。两者并存。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from jwt import InvalidTokenError

from ariadne.auth.rbac import Role
from ariadne.config import get_settings

_ALGORITHM = "HS256"


@dataclass(frozen=True)
class SessionClaims:
    """JWT 解码后的声明。"""

    project_id: str
    role: Role
    expires_at: datetime


def create_session_token(
    project_id: str,
    role: Role | str,
    *,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """签发 JWT 会话令牌。"""
    api = get_settings().api
    now = datetime.now(UTC)
    role_val = role.value if isinstance(role, Role) else str(role)
    payload: dict[str, Any] = {
        "iss": api.jwt_issuer,
        "sub": project_id,
        "role": role_val,
        "iat": now,
        "exp": now + timedelta(hours=api.jwt_ttl_hours),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, api.jwt_secret.get_secret_value(), algorithm=_ALGORITHM)


def verify_session_token(token: str) -> SessionClaims:
    """验证 JWT，返回声明。无效则 raise ValueError。"""
    api = get_settings().api
    try:
        payload = jwt.decode(
            token,
            api.jwt_secret.get_secret_value(),
            algorithms=[_ALGORITHM],
            options={"require": ["exp", "iat", "sub", "role"]},
        )
    except InvalidTokenError as exc:
        raise ValueError(f"无效的会话令牌: {exc}") from exc

    return SessionClaims(
        project_id=str(payload["sub"]),
        role=Role(str(payload["role"])),
        expires_at=datetime.fromtimestamp(
            int(payload["exp"]), tz=UTC
        ),
    )


__all__ = ["SessionClaims", "create_session_token", "verify_session_token"]
