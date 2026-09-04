"""跨租户 Worker 的租户枚举辅助。

RLS 的语义是"未设租户变量就看不到行"，这对 API 是安全，对 Worker 是
功能性障碍：retention / loop 补偿这类后台扫描必须先知道有哪些租户，
再逐租户建立会话。projects 表**没有** RLS —— 它是租户边界的注册表，
租户列表对认证后的服务可见是有意设计。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from ariadne.storage.postgres.models import Project

__all__ = ["list_project_ids"]


async def list_project_ids(pg: Any) -> list[UUID]:
    """列出全部租户 id（projects 表无 RLS，普通会话即可读）。"""
    async with pg.session() as session:
        result = await session.execute(select(Project.id))
        return list(result.scalars().all())
