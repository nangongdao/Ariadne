"""graphs 仓储。

Graph 定义的 CRUD 操作（简化版，只提供 Worker 需要的方法）。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ariadne.storage.postgres.graph_models import GraphRow


class GraphNotFoundError(LookupError):
    """Graph 不存在。"""


class GraphRepository:
    """Graph 仓储（简化版，只提供 get 方法用于 Worker）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self,
        project_id: uuid.UUID,
        graph_id: uuid.UUID,
    ) -> GraphRow:
        """查询 graph（需要 project_id 用于 RLS）。"""
        stmt = select(GraphRow).where(
            GraphRow.id == graph_id,
            GraphRow.project_id == project_id,
            GraphRow.is_active.is_(True),
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            raise GraphNotFoundError(f"Graph 不存在: {graph_id}")
        return row

    async def create(
        self,
        project_id: uuid.UUID,
        name: str,
        graph: dict[str, Any],
        description: str = "",
    ) -> uuid.UUID:
        """创建 graph，返回 ID。"""
        row = GraphRow(
            id=uuid.uuid4(),
            project_id=project_id,
            name=name,
            version=1,
            graph=graph,
            validation_errors=[],
            is_active=True,
            description=description,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id


__all__ = ["GraphNotFoundError", "GraphRepository"]
