"""RBAC 权限矩阵 —— 五角色 + 七权限。

M6 §4.1 的核心约束：approver 与 developer 必须可分离。
写代码的人不应能批准自己触发的高敏感操作。

权限矩阵是声明式的 —— 角色 → 权限集合的映射。
检查时只需查表，不需要逻辑分支。
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


class Role(StrEnum):
    """五角色。"""

    ADMIN = "admin"
    APPROVER = "approver"
    DEVELOPER = "developer"
    VIEWER = "viewer"
    BILLING = "billing"


class Permission(StrEnum):
    """七权限。"""

    READ = "read"
    WRITE = "write"
    APPROVE = "approve"
    DELETE = "delete"
    MANAGE_KEYS = "manage_keys"
    VIEW_BILLING = "view_billing"
    MANAGE_RULES = "manage_rules"


# 权限矩阵：角色 → 权限集合
# 关键约束（§4.1）：APPROVER 只有 APPROVE，无任何 WRITE
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),  # 全权限
    Role.APPROVER: frozenset({Permission.READ, Permission.APPROVE}),
    Role.DEVELOPER: frozenset(
        {
            Permission.READ,
            Permission.WRITE,
            Permission.DELETE,
            Permission.MANAGE_RULES,
        }
    ),
    Role.VIEWER: frozenset({Permission.READ}),
    Role.BILLING: frozenset({Permission.READ, Permission.VIEW_BILLING}),
}


def has_permission(role: Role, perm: Permission) -> bool:
    """检查角色是否有指定权限。"""
    return perm in ROLE_PERMISSIONS.get(role, frozenset())


def check_permission(role: Role, perm: Permission) -> None:
    """检查权限，不通过则 raise ForbiddenError。"""
    if not has_permission(role, perm):
        from ariadne.api.errors import ForbiddenError

        raise ForbiddenError(
            f"角色 {role.value} 无权限: {perm.value}",
            role=role.value,
            permission=perm.value,
        )


def role_permissions(role: Role) -> frozenset[Permission]:
    """返回角色的全部权限。"""
    return ROLE_PERMISSIONS.get(role, frozenset())


__all__ = [
    "ROLE_PERMISSIONS",
    "Permission",
    "Role",
    "check_permission",
    "has_permission",
    "role_permissions",
]
