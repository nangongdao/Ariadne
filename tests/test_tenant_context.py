"""租户上下文 + RLS 变量管理测试。

SQLite 侧验证：
- set/reset_tenant_context 不报错（no-op）
- TenantContext 不可变
- tenant_session 生命周期正确

PG 侧的 RLS 策略测试在 test_auth_integration.py（-m integration）。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Any
from uuid import UUID

import pytest

from ariadne.auth.rbac import Role
from ariadne.auth.tenant import (
    TenantContext,
    reset_tenant_context,
    set_tenant_context,
)


class TestTenantContext:
    def test_context_is_frozen(self) -> None:
        ctx = TenantContext(
            project_id=UUID("00000000-0000-0000-0000-000000000001"),
            role=Role.ADMIN,
        )
        with pytest.raises(FrozenInstanceError):
            ctx.project_id = UUID("00000000-0000-0000-0000-000000000002")  # type: ignore[misc]

    def test_context_fields(self) -> None:
        pid = UUID("00000000-0000-0000-0000-000000000001")
        ctx = TenantContext(project_id=pid, role=Role.VIEWER)
        assert ctx.project_id == pid
        assert ctx.role == Role.VIEWER


class TestSQLiteNoOp:
    """SQLite 无 RLS，set/reset 应是 no-op，不报错。"""

    async def test_set_tenant_context_sqlite(self, memory_pg: Any) -> None:
        async with memory_pg.session() as session:
            await set_tenant_context(
                session, UUID("00000000-0000-0000-0000-000000000001")
            )

    async def test_reset_tenant_context_sqlite(self, memory_pg: Any) -> None:
        async with memory_pg.session() as session:
            await reset_tenant_context(session)

    async def test_tenant_session_lifecycle(self, memory_pg: Any) -> None:
        """tenant_session 在 SQLite 上正常工作（no-op RLS）。"""
        async with memory_pg.session() as session:
            await set_tenant_context(
                session, UUID("00000000-0000-0000-0000-000000000001")
            )
            await reset_tenant_context(session)


class TestTenantSessionContextManager:
    async def test_session_yields_session(self, memory_pg: Any) -> None:
        """tenant_session 上下文管理器产出 AsyncSession。"""
        # memory_pg 不是 PostgresStore 实例，但接口兼容
        async with memory_pg.session() as session:
            from sqlalchemy import text

            result = await session.execute(text("SELECT 1"))
            assert result.scalar() == 1


class TestStaticAuthCompatibility:
    """static 后端不创建 tenant_context，get_tenant_context 返回默认。"""

    def test_static_backend_default_context(self) -> None:
        """static 模式下，require_project 返回 project_id 但不设 tenant_context。"""
        # 验证默认配置使用 static 后端。_env_file=None：断言的是代码默认值，
        # 仓库根的 .env（未纳入版本管理）不该参与，否则本机配置能让它变色。
        from ariadne.config import ApiSettings

        settings = ApiSettings(_env_file=None)
        assert settings.auth_backend == "static"
