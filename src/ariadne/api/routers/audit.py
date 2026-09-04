"""审计日志读取端点。

GET /v1/audit — 列出审计记录，支持按 loop_id 过滤 + 时间范围。
需要 READ 权限。

审计记录是 append-only（DB 侧 REVOKE UPDATE/DELETE），
此处只读不写。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ariadne.api.deps import TenantCtx, TenantPg
from ariadne.auth.rbac import Permission, check_permission

router = APIRouter(tags=["audit"])


class AuditEntry(BaseModel):
    """单条审计记录。"""

    id: UUID
    project_id: UUID
    loop_id: UUID | None
    hook: str
    action: str
    rule_hits: list[dict[str, Any]]
    winning_hit: dict[str, Any] | None
    context_snapshot: dict[str, Any]
    message: str
    created_at: datetime


class AuditListResponse(BaseModel):
    """审计记录列表响应。"""

    entries: list[AuditEntry]
    total: int


@router.get("/audit", response_model=AuditListResponse)
async def list_audit(
    ctx: TenantCtx,
    pg: TenantPg,
    loop_id: Annotated[UUID | None, Query(description="按 Loop ID 过滤")] = None,
    hours: Annotated[int, Query(ge=1, le=24 * 90, description="回溯小时数")] = 24,
    limit: Annotated[int, Query(ge=1, le=500, description="最大返回条数")] = 100,
) -> AuditListResponse:
    """列出当前项目的审计记录。需要 READ 权限。"""
    check_permission(ctx.role, Permission.READ)

    from sqlalchemy import desc, select

    from ariadne.storage.postgres.harness_models import AuditLogRow

    since = datetime.now(UTC) - timedelta(hours=hours)

    async with pg.session() as session:
        stmt = (
            select(AuditLogRow)
            .where(
                AuditLogRow.project_id == ctx.project_id,
                AuditLogRow.created_at >= since,
            )
            .order_by(desc(AuditLogRow.created_at))
            .limit(limit)
        )
        if loop_id is not None:
            stmt = stmt.where(AuditLogRow.loop_id == loop_id)
        result = await session.execute(stmt)
        rows = result.scalars().all()

    entries = [
        AuditEntry(
            id=row.id,
            project_id=row.project_id,
            loop_id=row.loop_id,
            hook=row.hook,
            action=row.action,
            rule_hits=row.rule_hits,
            winning_hit=row.winning_hit,
            context_snapshot=row.context_snapshot,
            message=row.message,
            created_at=row.created_at,
        )
        for row in rows
    ]

    return AuditListResponse(entries=entries, total=len(entries))


__all__ = ["router"]
