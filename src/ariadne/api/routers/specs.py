"""Spec 管理 API 路由。

- POST /v1/specs — 上传/创建 spec（校验 + 存储）
- GET /v1/specs — 列出项目 spec
- GET /v1/specs/{id} — 读取单个 spec
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ariadne.api.deps import TenantCtx, TenantPg
from ariadne.api.errors import BadRequestError, NotFoundError, UnprocessableError
from ariadne.auth.rbac import Permission, check_permission
from ariadne.spec_module import (
    SpecLoadError,
    load_spec_from_dict,
    validate_spec,
)

router = APIRouter(tags=["specs"])


# ---------- 请求/响应模型 ----------


class SpecCreateRequest(BaseModel):
    """创建 spec 请求。spec 内容直接内联。"""

    spec: dict[str, Any] = Field(...)


class SpecResponse(BaseModel):
    """spec 响应。"""

    id: UUID
    project_id: UUID
    version: int
    spec: dict[str, Any]


# ---------- 端点 ----------


@router.post("/specs", response_model=SpecResponse, status_code=201)
async def create_spec(
    body: SpecCreateRequest,
    ctx: TenantCtx,
    pg: TenantPg,
) -> SpecResponse:
    """创建 spec：校验 + 存储。

    校验 spec.yaml schema（Pydantic）+ 目标可验证性（validate_spec）。
    不可验证的 spec 返回 422。
    """
    check_permission(ctx.role, Permission.WRITE)
    # 加载 + Pydantic 校验
    try:
        spec = load_spec_from_dict(body.spec)
    except SpecLoadError as exc:
        raise BadRequestError(f"spec 校验失败: {exc}") from exc

    # 目标可验证性校验
    # API 层假定沙箱可配置（部署时启用），因此 sandbox_available=True
    report = validate_spec(spec, sandbox_available=True)
    if not report.ok:
        raise UnprocessableError(
            "目标不可验证", reasons=[i.message for i in report.errors]
        )

    # 存储
    import uuid as uuid_mod

    from ariadne.storage.postgres.harness_models import RuleSetRow

    # spec 存为规则集的一种特殊形式（rules 来自 spec.rules）
    rules_data = [r.model_dump() for r in spec.rules]
    async with pg.session() as session:
        row = RuleSetRow(
            id=uuid_mod.uuid4(),
            project_id=ctx.project_id,
            name="spec",
            version=1,
            rules=rules_data,
            is_active=True,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

        return SpecResponse(
            id=row.id,
            project_id=row.project_id,
            version=row.version,
            spec=body.spec,
        )


@router.get("/specs", response_model=list[SpecResponse])
async def list_specs(
    ctx: TenantCtx,
    pg: TenantPg,
) -> list[SpecResponse]:
    """列出项目下所有 spec。"""
    check_permission(ctx.role, Permission.READ)
    from sqlalchemy import select

    from ariadne.storage.postgres.harness_models import RuleSetRow

    async with pg.session() as session:
        stmt = (
            select(RuleSetRow)
            .where(
                RuleSetRow.project_id == ctx.project_id,
                RuleSetRow.name == "spec",
            )
            .order_by(RuleSetRow.created_at.desc())
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()
        return [
            SpecResponse(
                id=row.id,
                project_id=row.project_id,
                version=row.version,
                spec={"rules": row.rules},
            )
            for row in rows
        ]


@router.get("/specs/{spec_id}", response_model=SpecResponse)
async def get_spec(
    spec_id: UUID,
    ctx: TenantCtx,
    pg: TenantPg,
) -> SpecResponse:
    """读取单个 spec。"""
    check_permission(ctx.role, Permission.READ)
    from sqlalchemy import select

    from ariadne.storage.postgres.harness_models import RuleSetRow

    async with pg.session() as session:
        stmt = select(RuleSetRow).where(
            RuleSetRow.id == spec_id,
            RuleSetRow.project_id == ctx.project_id,
        )
        result = await session.execute(stmt)
        row = result.scalars().first()

        if row is None:
            raise NotFoundError(f"spec {spec_id} 不存在", spec_id=str(spec_id))

        return SpecResponse(
            id=row.id,
            project_id=row.project_id,
            version=row.version,
            spec={"rules": row.rules},
        )


__all__ = ["router"]
