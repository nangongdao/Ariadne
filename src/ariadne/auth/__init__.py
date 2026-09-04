"""认证与授权模块（M6）。

- rbac.py — 五角色 + 权限矩阵
- keys.py — Argon2id API Key 哈希校验
- jwt.py — JWT Web Console 会话
- tenant.py — 租户上下文 + RLS 变量管理
- key_lookup.py — 前缀 -> project_id 跨租户解析（认证的第一步）
- rls.py — 启动自检：RLS 是否真的对当前角色生效
"""

from ariadne.auth.jwt import SessionClaims, create_session_token, verify_session_token
from ariadne.auth.key_lookup import resolve_projects_by_prefix
from ariadne.auth.keys import (
    extract_prefix,
    generate_api_key,
    hash_api_key,
    verify_api_key,
)
from ariadne.auth.rbac import (
    ROLE_PERMISSIONS,
    Permission,
    Role,
    check_permission,
    has_permission,
    role_permissions,
)
from ariadne.auth.rls import RlsReport, TableStatus, check_rls, verify_rls
from ariadne.auth.tenant import (
    TenantContext,
    reset_tenant_context,
    set_tenant_context,
    tenant_session,
)

__all__ = [
    "ROLE_PERMISSIONS",
    "Permission",
    "RlsReport",
    "Role",
    "SessionClaims",
    "TableStatus",
    "TenantContext",
    "check_permission",
    "check_rls",
    "create_session_token",
    "extract_prefix",
    "generate_api_key",
    "has_permission",
    "hash_api_key",
    "reset_tenant_context",
    "resolve_projects_by_prefix",
    "role_permissions",
    "set_tenant_context",
    "tenant_session",
    "verify_api_key",
    "verify_rls",
    "verify_session_token",
]
