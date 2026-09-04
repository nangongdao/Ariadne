"""API Key 管理 API 路由。

- POST /v1/keys — 创建 API Key（返回明文一次）
- GET /v1/keys — 列出当前项目的 keys
- DELETE /v1/keys/{key_id} — 吊销 key

需要 MANAGE_KEYS 权限（admin 角色默认拥有）。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ariadne.api.deps import TenantCtx, TenantPg
from ariadne.api.errors import BadRequestError, NotFoundError
from ariadne.auth.rbac import Permission, Role, check_permission
from ariadne.storage.postgres.repositories.api_keys import (
    ApiKeyNotFoundError,
    ApiKeyRepository,
)

router = APIRouter(tags=["keys"])


# ---------- 请求/响应模型 ----------


class KeyCreateRequest(BaseModel):
    """创建 API Key 请求。"""

    name: str = Field(min_length=1, max_length=200)
    role: str = Field(default="viewer", max_length=20)
    expires_at: datetime | None = None


class KeyCreateResponse(BaseModel):
    """创建 API Key 响应。

    key 字段是明文 API Key，仅在创建时返回一次。
    之后只能看到 key_prefix（展示用）。
    """

    id: UUID
    key: str  # 明文，仅此一次
    key_prefix: str
    name: str
    role: str
    expires_at: datetime | None


class KeyResponse(BaseModel):
    """API Key 列表项（不含哈希和明文）。"""

    id: UUID
    key_prefix: str
    name: str
    role: str
    is_active: bool
    last_used_at: datetime | None
    expires_at: datetime | None
    created_at: datetime


class KeyListResponse(BaseModel):
    keys: list[KeyResponse]


# ---------- 路由 ----------


@router.post("/keys", response_model=KeyCreateResponse, status_code=201)
async def create_key(
    body: KeyCreateRequest,
    ctx: TenantCtx,
    pg: TenantPg,
) -> KeyCreateResponse:
    """创建 API Key。返回明文 key —— 仅此一次，不再次展示。"""
    check_permission(ctx.role, Permission.MANAGE_KEYS)

    # 校验角色有效
    try:
        Role(body.role)
    except ValueError:
        raise BadRequestError(f"无效的角色: {body.role}") from None

    async with pg.session() as session:
        repo = ApiKeyRepository(session)
        key_id, plain_key = await repo.create(
            project_id=ctx.project_id,
            name=body.name,
            role=body.role,
            expires_at=body.expires_at,
        )

    return KeyCreateResponse(
        id=key_id,
        key=plain_key,
        key_prefix=plain_key[:16],
        name=body.name,
        role=body.role,
        expires_at=body.expires_at,
    )


@router.get("/keys", response_model=KeyListResponse)
async def list_keys(
    ctx: TenantCtx,
    pg: TenantPg,
) -> KeyListResponse:
    """列出当前项目的 API Keys。需要 MANAGE_KEYS 权限。"""
    check_permission(ctx.role, Permission.MANAGE_KEYS)

    async with pg.session() as session:
        repo = ApiKeyRepository(session)
        rows = await repo.list(project_id=ctx.project_id)

    return KeyListResponse(
        keys=[
            KeyResponse(
                id=row.id,
                key_prefix=row.key_prefix,
                name=row.name,
                role=row.role,
                is_active=row.is_active,
                last_used_at=row.last_used_at,
                expires_at=row.expires_at,
                created_at=row.created_at,
            )
            for row in rows
        ]
    )


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_key(
    key_id: UUID,
    ctx: TenantCtx,
    pg: TenantPg,
) -> None:
    """吊销 API Key。需要 MANAGE_KEYS 权限。"""
    check_permission(ctx.role, Permission.MANAGE_KEYS)

    async with pg.session() as session:
        repo = ApiKeyRepository(session)
        try:
            await repo.revoke(project_id=ctx.project_id, key_id=key_id)
        except ApiKeyNotFoundError:
            raise NotFoundError(f"API Key {key_id} 不存在") from None


__all__ = ["router"]
