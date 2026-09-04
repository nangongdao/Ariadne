"""RBAC 权限矩阵测试 —— 五角色 + 七权限。"""

from __future__ import annotations

import pytest

from ariadne.api.errors import ForbiddenError
from ariadne.auth.rbac import (
    ROLE_PERMISSIONS,
    Permission,
    Role,
    check_permission,
    has_permission,
    role_permissions,
)


class TestPermissionMatrix:
    def test_admin_has_all_permissions(self) -> None:
        for perm in Permission:
            assert has_permission(Role.ADMIN, perm), f"admin 缺少 {perm}"

    def test_viewer_has_only_read(self) -> None:
        assert has_permission(Role.VIEWER, Permission.READ)
        assert not has_permission(Role.VIEWER, Permission.WRITE)
        assert not has_permission(Role.VIEWER, Permission.DELETE)
        assert not has_permission(Role.VIEWER, Permission.APPROVE)
        assert not has_permission(Role.VIEWER, Permission.MANAGE_KEYS)
        assert not has_permission(Role.VIEWER, Permission.VIEW_BILLING)
        assert not has_permission(Role.VIEWER, Permission.MANAGE_RULES)

    def test_developer_has_write_but_not_approve(self) -> None:
        assert has_permission(Role.DEVELOPER, Permission.READ)
        assert has_permission(Role.DEVELOPER, Permission.WRITE)
        assert has_permission(Role.DEVELOPER, Permission.DELETE)
        assert has_permission(Role.DEVELOPER, Permission.MANAGE_RULES)
        assert not has_permission(Role.DEVELOPER, Permission.APPROVE)
        assert not has_permission(Role.DEVELOPER, Permission.MANAGE_KEYS)
        assert not has_permission(Role.DEVELOPER, Permission.VIEW_BILLING)

    def test_approver_has_only_read_and_approve(self) -> None:
        """§4.1 关键约束：approver 只有 APPROVE，无任何 WRITE。"""
        assert has_permission(Role.APPROVER, Permission.READ)
        assert has_permission(Role.APPROVER, Permission.APPROVE)
        assert not has_permission(Role.APPROVER, Permission.WRITE)
        assert not has_permission(Role.APPROVER, Permission.DELETE)
        assert not has_permission(Role.APPROVER, Permission.MANAGE_KEYS)
        assert not has_permission(Role.APPROVER, Permission.MANAGE_RULES)
        assert not has_permission(Role.APPROVER, Permission.VIEW_BILLING)

    def test_billing_has_read_and_billing(self) -> None:
        assert has_permission(Role.BILLING, Permission.READ)
        assert has_permission(Role.BILLING, Permission.VIEW_BILLING)
        assert not has_permission(Role.BILLING, Permission.WRITE)
        assert not has_permission(Role.BILLING, Permission.APPROVE)

    def test_approver_cannot_write(self) -> None:
        """职责分离：approver 不能写，不能管理 key，不能管理规则。"""
        approver_denied = [
            Permission.WRITE,
            Permission.DELETE,
            Permission.MANAGE_KEYS,
            Permission.MANAGE_RULES,
        ]
        for perm in approver_denied:
            assert not has_permission(Role.APPROVER, perm)

    def test_all_roles_have_read(self) -> None:
        for role in Role:
            assert has_permission(role, Permission.READ), f"{role} 缺少 READ"

    def test_all_permissions_assigned(self) -> None:
        """每个权限至少有一个角色拥有。"""
        for perm in Permission:
            assigned = any(perm in perms for perms in ROLE_PERMISSIONS.values())
            assert assigned, f"权限 {perm} 未分配给任何角色"

    def test_role_permissions_returns_correct_set(self) -> None:
        perms = role_permissions(Role.VIEWER)
        assert perms == frozenset({Permission.READ})

    def test_role_permissions_unknown_role(self) -> None:
        perms = role_permissions(Role.ADMIN)  # just testing lookup
        assert Permission.READ in perms


class TestCheckPermission:
    def test_check_permission_passes(self) -> None:
        check_permission(Role.ADMIN, Permission.WRITE)  # 不 raise

    def test_check_permission_raises(self) -> None:
        with pytest.raises(ForbiddenError) as exc_info:
            check_permission(Role.VIEWER, Permission.WRITE)
        assert "viewer" in str(exc_info.value.detail)
        assert exc_info.value.extra["role"] == "viewer"
        assert exc_info.value.extra["permission"] == "write"

    def test_check_permission_approver_write_raises(self) -> None:
        with pytest.raises(ForbiddenError):
            check_permission(Role.APPROVER, Permission.WRITE)

    def test_check_permission_approver_approve_passes(self) -> None:
        check_permission(Role.APPROVER, Permission.APPROVE)  # 不 raise

    def test_forbidden_error_status_code(self) -> None:
        assert ForbiddenError.status_code == 403
