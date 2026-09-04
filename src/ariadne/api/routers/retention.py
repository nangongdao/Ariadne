"""GDPR 数据删除 API 路由。

DELETE /v1/projects/{id}/data — 发起级联删除请求
GET /v1/projects/{id}/deletion-jobs — 查看删除任务列表

M6 §4.3：ClickHouse 的 ALTER TABLE DELETE 是异步 mutation，
API 返回 202 + job_id，不声称"已删除"。
文档承诺最长 24 小时内完成。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field

from ariadne.api.deps import TenantCtx, TenantPg
from ariadne.api.errors import ForbiddenError
from ariadne.auth.rbac import Permission, check_permission
from ariadne.storage.postgres.retention_models import DeletionJobRow

router = APIRouter(tags=["retention"])


class DeletionRequest(BaseModel):
    """GDPR 删除请求体。"""

    subject_id: str = Field(default="", max_length=256)


class DeletionJobResponse(BaseModel):
    """删除任务状态响应。"""

    id: UUID
    project_id: UUID
    subject_id: str
    status: str
    error: str
    clickhouse_mutation_id: str
    postgres_deleted: int
    clickhouse_deleted: int
    s3_deleted: int
    started_at: datetime
    updated_at: datetime


@router.delete(
    "/projects/{project_id}/data",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DeletionJobResponse,
    summary="发起 GDPR 数据删除请求",
)
async def request_data_deletion(
    project_id: UUID,
    ctx: TenantCtx,
    pg: TenantPg,
    body: DeletionRequest | None = None,
) -> DeletionJobResponse:
    """登记级联删除任务（Postgres + ClickHouse + S3）。

    此端点只**登记**任务并返回 202 + job_id，实际删除由 Retention Worker
    （`ariadne-worker retention`）异步执行。ClickHouse 的 ALTER TABLE DELETE
    是异步 mutation，同步删完不可能。

    调用方轮询 GET /v1/projects/{id}/deletion-jobs 查看进度：
    pending → postgres_done → clickhouse_mutation_submitted → s3_done → completed

    **部署要求**：没有部署 Retention Worker 时任务会永久停在 pending ——
    此时 202 是"已登记"而非"会删除"，合规上不成立。见 docs/M6-spec §4.3。

    需要 DELETE 权限（仅 admin 角色）。
    """
    if project_id != ctx.project_id:
        raise ForbiddenError("不能操作其他项目的数据", project_id=str(project_id))
    check_permission(ctx.role, Permission.DELETE)

    from uuid import uuid4

    subject_id = body.subject_id if body else ""

    async with pg.session() as session:
        row = DeletionJobRow(
            id=uuid4(),
            project_id=ctx.project_id,
            subject_id=subject_id,
            status="pending",
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

        return DeletionJobResponse(
            id=row.id,
            project_id=row.project_id,
            subject_id=row.subject_id,
            status=row.status,
            error=row.error,
            clickhouse_mutation_id=row.clickhouse_mutation_id,
            postgres_deleted=row.postgres_deleted,
            clickhouse_deleted=row.clickhouse_deleted,
            s3_deleted=row.s3_deleted,
            started_at=row.created_at,
            updated_at=row.updated_at,
        )


@router.get(
    "/projects/{project_id}/deletion-jobs",
    response_model=list[DeletionJobResponse],
    summary="查看删除任务列表",
)
async def list_deletion_jobs(
    project_id: UUID,
    ctx: TenantCtx,
    pg: TenantPg,
    limit: Annotated[int, Query(ge=1, le=100, description="最大返回条数")] = 20,
) -> list[DeletionJobResponse]:
    """列出当前项目的删除任务。需要 READ 权限。"""
    if project_id != ctx.project_id:
        raise ForbiddenError("不能读取其他项目的删除任务", project_id=str(project_id))
    check_permission(ctx.role, Permission.READ)

    from sqlalchemy import desc, select

    async with pg.session() as session:
        stmt = (
            select(DeletionJobRow)
            .where(DeletionJobRow.project_id == ctx.project_id)
            .order_by(desc(DeletionJobRow.created_at))
            .limit(limit)
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

    return [
        DeletionJobResponse(
            id=row.id,
            project_id=row.project_id,
            subject_id=row.subject_id,
            status=row.status,
            error=row.error,
            clickhouse_mutation_id=row.clickhouse_mutation_id,
            postgres_deleted=row.postgres_deleted,
            clickhouse_deleted=row.clickhouse_deleted,
            s3_deleted=row.s3_deleted,
            started_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in rows
    ]


__all__ = ["router"]
