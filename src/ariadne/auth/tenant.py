"""租户上下文 + RLS 变量管理。

M6 §3 的核心：RLS 不能只靠应用层过滤。连接池在每次取用连接时
SET LOCAL ariadne.project_id = ...，连接归还前必须 RESET ——
否则连接复用会导致租户串号（RLS 最常见的实现错误）。

SET LOCAL 是事务作用域，事务结束自动清理。但仍显式 RESET 作为防御：
如果连接池在事务未提交时归还连接，SET LOCAL 可能残留。

SQLite 侧：SET LOCAL / RESET 是 no-op（SQLite 无 RLS），不报错。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ariadne.auth.rbac import Role
from ariadne.storage.postgres.engine import PostgresStore
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

_GUC_NAME = "ariadne.project_id"


@dataclass(frozen=True)
class TenantContext:
    """请求级租户上下文。

    携带 project_id（数据隔离）和 role（权限检查）。
    在 require_tenant 依赖中创建，挂载到 request.state.tenant_context。
    """

    project_id: UUID
    role: Role


async def set_tenant_context(session: AsyncSession, project_id: UUID) -> None:
    """设置 RLS 变量。

    用 set_config(..., is_local=true) 而非 SET LOCAL：语义等价（都是事务
    作用域），但 SET LOCAL 的值位置不接受绑定参数，只能拼字符串。
    仍需在连接归还前显式 RESET（见 reset_tenant_context）。
    """
    # SQLite 不支持事务级 GUC，静默跳过
    dialect = session.bind.dialect.name if session.bind else "unknown"
    if dialect == "sqlite":
        return
    await session.execute(
        text(f"SELECT set_config('{_GUC_NAME}', :project_id, true)"),
        {"project_id": str(project_id)},
    )


async def reset_tenant_context(session: AsyncSession) -> None:
    """重置 RLS 变量。

    连接归还连接池前必须调用，否则复用时变量残留导致租户串号。
    """
    dialect = session.bind.dialect.name if session.bind else "unknown"
    if dialect == "sqlite":
        return
    await session.execute(text(f"RESET {_GUC_NAME}"))


@asynccontextmanager
async def tenant_session(
    pg: PostgresStore,
    project_id: UUID,
) -> AsyncIterator[AsyncSession]:
    """带租户上下文的会话。

    取 session → SET LOCAL → yield → RESET → 关闭。
    确保 RLS 变量不会泄漏到下一个复用连接。
    """
    async with pg.session() as session:
        try:
            await set_tenant_context(session, project_id)
            yield session
        finally:
            try:
                await reset_tenant_context(session)
            except Exception:
                # RESET 失败不应阻塞业务逻辑，但要记录日志
                logger.warning("RLS 变量重置失败", extra={"project_id": str(project_id)})


__all__ = [
    "TenantContext",
    "reset_tenant_context",
    "set_tenant_context",
    "tenant_session",
]
