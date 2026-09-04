"""HITL 审批 API 路由。

- POST /v1/loops/{loop_id}/approvals — 提交审批请求
- GET /v1/loops/{loop_id}/approvals — 列出审批
- POST /v1/approvals/{id}/decide — 批准/拒绝
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ariadne.api.deps import AppSettings, TenantCtx, TenantPg
from ariadne.api.errors import BadRequestError, NotFoundError
from ariadne.auth.rbac import Permission, check_permission

router = APIRouter(tags=["approvals"])


# ---------- 请求/响应模型 ----------


class ApprovalCreateRequest(BaseModel):
    """提交审批请求。"""

    context: dict[str, Any] = Field(default_factory=dict)
    expires_in_seconds: int = Field(default=3600, ge=1, le=86400)


class ApprovalDecideRequest(BaseModel):
    """批准/拒绝审批。"""

    decision: str = Field(..., pattern="^(approved|rejected)$")
    reviewer: str = Field(min_length=1)
    comment: str = ""


class ApprovalResponse(BaseModel):
    """审批响应。"""

    id: UUID
    loop_id: UUID
    status: str
    context: dict[str, Any]
    reviewer: str | None = None
    comment: str = ""
    expires_at: str | None = None
    decided_at: str | None = None
    created_at: str = ""


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite 读出的 datetime 是 naive 的，补上 UTC 才能与 now(UTC) 比较。"""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


# ---------- 端点 ----------


@router.post(
    "/loops/{loop_id}/approvals",
    response_model=ApprovalResponse,
    status_code=201,
)
async def create_approval(
    loop_id: UUID,
    body: ApprovalCreateRequest,
    ctx: TenantCtx,
    pg: TenantPg,
) -> ApprovalResponse:
    """提交审批请求。"""
    check_permission(ctx.role, Permission.WRITE)
    import uuid as uuid_mod

    from sqlalchemy import select

    from ariadne.storage.postgres.harness_models import ApprovalRow
    from ariadne.storage.postgres.loop_models import LoopRun

    async with pg.session() as session:
        # 验证 loop 存在
        stmt = select(LoopRun).where(
            LoopRun.id == loop_id,
            LoopRun.project_id == ctx.project_id,
        )
        result = await session.execute(stmt)
        loop = result.scalars().first()
        if loop is None:
            raise NotFoundError(f"loop {loop_id} 不存在", loop_id=str(loop_id))

        expires_at = datetime.now(UTC).replace(microsecond=0) + __import__(
            "datetime"
        ).timedelta(seconds=body.expires_in_seconds)

        row = ApprovalRow(
            id=uuid_mod.uuid4(),
            project_id=ctx.project_id,
            loop_id=loop_id,
            status="pending",
            context=body.context,
            expires_at=expires_at,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

        return _to_response(row)


@router.get("/loops/{loop_id}/approvals", response_model=list[ApprovalResponse])
async def list_approvals(
    loop_id: UUID,
    ctx: TenantCtx,
    pg: TenantPg,
) -> list[ApprovalResponse]:
    """列出某 loop 的所有审批。"""
    check_permission(ctx.role, Permission.READ)
    from sqlalchemy import select

    from ariadne.storage.postgres.harness_models import ApprovalRow

    async with pg.session() as session:
        # 过期审批自动标记 expired
        now = datetime.now(UTC)
        stmt = select(ApprovalRow).where(
            ApprovalRow.loop_id == loop_id,
            ApprovalRow.project_id == ctx.project_id,
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()

        updated: list[ApprovalResponse] = []
        for row in rows:
            expires_at = _as_utc(row.expires_at)
            if row.status == "pending" and expires_at is not None and expires_at < now:
                row.status = "expired"
            updated.append(_to_response(row))
        await session.commit()
        return updated


@router.post("/approvals/{approval_id}/decide", response_model=ApprovalResponse)
async def decide_approval(
    approval_id: UUID,
    body: ApprovalDecideRequest,
    ctx: TenantCtx,
    pg: TenantPg,
    settings: AppSettings,
) -> ApprovalResponse:
    """批准或拒绝审批。"""
    check_permission(ctx.role, Permission.APPROVE)
    from sqlalchemy import select

    from ariadne.storage.postgres.harness_models import ApprovalRow

    async with pg.session() as session:
        stmt = select(ApprovalRow).where(
            ApprovalRow.id == approval_id,
            ApprovalRow.project_id == ctx.project_id,
        ).with_for_update()
        result = await session.execute(stmt)
        row = result.scalars().first()

        if row is None:
            raise NotFoundError(
                f"approval {approval_id} 不存在", approval_id=str(approval_id)
            )

        if row.status != "pending":
            raise BadRequestError(f"审批已处理，当前状态: {row.status}")

        # 过期检查（SQLite 读出的 expires_at 是 naive，先归一）
        now = datetime.now(UTC)
        expires_at = _as_utc(row.expires_at)
        if expires_at is not None and expires_at < now:
            row.status = "expired"
            await session.commit()
            raise BadRequestError("审批已过期")

        row.status = body.decision
        row.reviewer = body.reviewer
        row.comment = body.comment
        row.decided_at = now
        await session.commit()
        await session.refresh(row)

        response = _to_response(row)

    # 把审批决定接回 Loop 状态机。只有真正处于 HUMAN_PENDING 的 Loop
    # 才推进，避免一个独立的审批记录意外改写正常运行中的 Loop。
    from ariadne.loop_module.state_machine import LoopState
    from ariadne.storage.postgres.repositories.loop_runs import (
        LoopRunConflictError,
        LoopRunRepository,
    )

    transitioned = False
    async with pg.session() as session:
        loop_repo = LoopRunRepository(session)
        try:
            loop = await loop_repo.get(project_id=ctx.project_id, loop_id=row.loop_id)
        except LookupError:
            loop = None
        if loop is not None and loop.state == LoopState.HUMAN_PENDING.value:
            try:
                await loop_repo.transition(
                    loop_id=row.loop_id,
                    project_id=ctx.project_id,
                    to=(
                        LoopState.EXECUTING
                        if body.decision == "approved"
                        else LoopState.REJECTED
                    ),
                    error="人工拒绝" if body.decision == "rejected" else "",
                )
                transitioned = True
            except LoopRunConflictError:
                # Loop 已被其他流程推进，审批记录仍保留，避免把决定吞掉。
                transitioned = False

    if transitioned and body.decision == "approved":
        from ariadne.api.routers.loops import _enqueue

        await _enqueue(settings, row.loop_id, ctx.project_id)

    return response


def _to_response(row: Any) -> ApprovalResponse:
    """ORM row → API response。"""
    return ApprovalResponse(
        id=row.id,
        loop_id=row.loop_id,
        status=row.status,
        context=row.context,
        reviewer=row.reviewer,
        comment=row.comment,
        expires_at=row.expires_at.isoformat() if row.expires_at else None,
        decided_at=row.decided_at.isoformat() if row.decided_at else None,
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


__all__ = ["router"]
