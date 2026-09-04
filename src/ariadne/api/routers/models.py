"""LLM 模型配置管理 API 路由。

- POST   /v1/models          — 创建模型配置（api_key 明文仅返回一次）
- GET    /v1/models          — 列出当前项目的模型配置（仅 api_key 前缀）
- GET    /v1/models/default   — 返回项目默认配置元数据
- PUT    /v1/models/{id}      — 更新（api_key 可选传，不传则保留）
- DELETE /v1/models/{id}      — 删除

需要 WRITE 权限（admin / developer 角色）。
Worker 在装配 Loop Engine 时直接通过租户数据库会话解析模型配置；该路由
只返回元数据，不回传 provider 密钥。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from ariadne.api.deps import TenantCtx, TenantPg
from ariadne.api.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
)
from ariadne.auth.rbac import Permission, check_permission
from ariadne.storage.postgres.model_config_models import LlmModelConfig
from ariadne.storage.postgres.repositories.model_configs import (
    ApiKeyDecryptionError,
    DuplicateLlmModelConfigError,
    LlmModelConfigNotFoundError,
    LlmModelConfigRepository,
    decrypt_api_key,
    is_cryptography_available,
)

router = APIRouter(tags=["models"])

# provider 白名单：openai_compatible 覆盖 Ollama / vLLM / OneAPI 等网关
_PROVIDERS = frozenset({"anthropic", "openai", "openai_compatible"})


def _encryption_secret() -> str:
    """加密密钥来源：settings.api.jwt_secret（生产/测试统一来源）。"""
    from ariadne.config import get_settings

    return get_settings().api.jwt_secret.get_secret_value()


def _previous_secrets() -> list[str]:
    """轮换期的历史加密密钥（仅解密用）。"""
    from ariadne.config import get_settings

    return list(get_settings().api.previous_jwt_secrets)


# ---------- 请求/响应模型 ----------


class ModelConfigCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    provider: str = Field(min_length=1, max_length=50)
    model: str = Field(min_length=1, max_length=200)
    api_key: str = Field(default="", max_length=2000)
    base_url: str = Field(default="", max_length=500)
    degraded_model: str = Field(default="", max_length=200)
    is_default: bool = False
    sort_order: int = Field(default=0, ge=0, le=10000)


class ModelConfigUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    provider: str | None = Field(default=None, min_length=1, max_length=50)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=2000)
    base_url: str | None = Field(default=None, max_length=500)
    degraded_model: str | None = Field(default=None, max_length=200)
    is_default: bool | None = None
    is_active: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=10000)


class ModelConfigResponse(BaseModel):
    """列表/详情响应。不含明文 api_key，仅前缀。"""

    id: uuid.UUID
    name: str
    provider: str
    model: str
    api_key_prefix: str
    base_url: str
    degraded_model: str
    is_default: bool
    is_active: bool
    sort_order: int
    last_used_at: datetime | None
    created_at: datetime


class ModelConfigCreateResponse(ModelConfigResponse):
    """创建响应额外带明文 api_key —— 仅此一次。"""

    api_key: str


class ModelConfigListResponse(BaseModel):
    models: list[ModelConfigResponse]
    cryptography_available: bool


class DefaultModelConfigResponse(BaseModel):
    """默认模型配置元数据。provider 密钥永远不通过读取接口返回。"""

    id: uuid.UUID
    name: str
    provider: str
    model: str
    base_url: str
    degraded_model: str
    found: bool = True


# ---------- 路由 ----------


def _validate_provider(provider: str) -> None:
    if provider not in _PROVIDERS:
        raise BadRequestError(
            f"不支持的 provider {provider!r}，当前支持: {sorted(_PROVIDERS)}"
        )


def _prefix_of(row: LlmModelConfig) -> str:
    """从加密存储还原展示前缀：解密后取前 12 字符。

    列表场景不暴露明文，但前缀需可读（让用户认出哪把 key）。
    解密失败不炸列表：返回哨兵前缀，把"这条配置需要重新录入"显示出来
    —— 静默显示密文或空串都会伪装成可用的配置。
    """
    if not row.api_key_encrypted:
        return ""
    try:
        plaintext = decrypt_api_key(
            row.api_key_encrypted,
            _encryption_secret(),
            previous_secrets=_previous_secrets(),
        )
    except ApiKeyDecryptionError:
        return "（密钥失效，请重新录入）"
    return plaintext[:12] if plaintext else ""


def _to_response(row: LlmModelConfig) -> ModelConfigResponse:
    return ModelConfigResponse(
        id=row.id,
        name=row.name,
        provider=row.provider,
        model=row.model,
        api_key_prefix=_prefix_of(row),
        base_url=row.base_url,
        degraded_model=row.degraded_model,
        is_default=row.is_default,
        is_active=row.is_active,
        sort_order=row.sort_order,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
    )


@router.post(
    "/models",
    response_model=ModelConfigCreateResponse,
    status_code=status.HTTP_201_CREATED,
    summary="创建模型配置",
)
async def create_model_config(
    body: ModelConfigCreateRequest,
    ctx: TenantCtx,
    pg: TenantPg,
) -> ModelConfigCreateResponse:
    """创建 LLM 模型配置。api_key 明文仅在此返回一次。"""
    check_permission(ctx.role, Permission.WRITE)
    _validate_provider(body.provider)

    async with pg.session() as session:
        repo = LlmModelConfigRepository(session)
        try:
            config_id, key_prefix = await repo.create(
                project_id=ctx.project_id,
                name=body.name,
                provider=body.provider,
                model=body.model,
                api_key=body.api_key,
                base_url=body.base_url,
                degraded_model=body.degraded_model,
                is_default=body.is_default,
                sort_order=body.sort_order,
                encryption_secret=_encryption_secret(),
            )
        except DuplicateLlmModelConfigError as exc:
            raise ConflictError(str(exc)) from exc

        # flush 后拿回完整行（含 created_at / is_default 调整后的值）
        row = await repo.get(project_id=ctx.project_id, config_id=config_id)
        assert row is not None

    return ModelConfigCreateResponse(
        id=row.id,
        name=row.name,
        provider=row.provider,
        model=row.model,
        api_key=body.api_key,
        api_key_prefix=key_prefix,
        base_url=row.base_url,
        degraded_model=row.degraded_model,
        is_default=row.is_default,
        is_active=row.is_active,
        sort_order=row.sort_order,
        last_used_at=row.last_used_at,
        created_at=row.created_at,
    )


@router.get("/models", response_model=ModelConfigListResponse, summary="列出模型配置")
async def list_model_configs(
    ctx: TenantCtx,
    pg: TenantPg,
) -> ModelConfigListResponse:
    """列出当前项目的模型配置。api_key 仅返回前缀。"""
    check_permission(ctx.role, Permission.READ)
    async with pg.session() as session:
        repo = LlmModelConfigRepository(session)
        rows = await repo.list(project_id=ctx.project_id)

    return ModelConfigListResponse(
        models=[_to_response(r) for r in rows],
        cryptography_available=is_cryptography_available(),
    )


@router.get(
    "/models/default",
    response_model=DefaultModelConfigResponse,
    summary="获取默认模型配置",
)
async def get_default_model_config(
    ctx: TenantCtx,
    pg: TenantPg,
) -> DefaultModelConfigResponse:
    """返回项目默认模型配置元数据。

    无默认配置时返回 found=False，调用方回退到环境变量。
    Worker 不调用此 HTTP 接口，密钥只在服务端租户会话内解密使用。
    """
    check_permission(ctx.role, Permission.READ)
    async with pg.session() as session:
        repo = LlmModelConfigRepository(session)
        row = await repo.get_default(project_id=ctx.project_id)

    if row is None:
        return DefaultModelConfigResponse(
            id=uuid.UUID(int=0),
            name="",
            provider="",
            model="",
            base_url="",
            degraded_model="",
            found=False,
        )

    return DefaultModelConfigResponse(
        id=row.id,
        name=row.name,
        provider=row.provider,
        model=row.model,
        base_url=row.base_url,
        degraded_model=row.degraded_model,
        found=True,
    )


@router.put(
    "/models/{config_id}",
    response_model=ModelConfigResponse,
    summary="更新模型配置",
)
async def update_model_config(
    config_id: uuid.UUID,
    body: ModelConfigUpdateRequest,
    ctx: TenantCtx,
    pg: TenantPg,
) -> ModelConfigResponse:
    """更新模型配置。api_key 传非 None 才更新；不传则保留原值。"""
    check_permission(ctx.role, Permission.WRITE)
    if body.provider is not None:
        _validate_provider(body.provider)

    async with pg.session() as session:
        repo = LlmModelConfigRepository(session)
        try:
            await repo.update(
                project_id=ctx.project_id,
                config_id=config_id,
                name=body.name,
                provider=body.provider,
                model=body.model,
                api_key=body.api_key,
                base_url=body.base_url,
                degraded_model=body.degraded_model,
                is_default=body.is_default,
                is_active=body.is_active,
                sort_order=body.sort_order,
                encryption_secret=_encryption_secret(),
            )
        except LlmModelConfigNotFoundError as exc:
            raise NotFoundError(str(exc)) from exc
        except DuplicateLlmModelConfigError as exc:
            raise ConflictError(str(exc)) from exc

        row = await repo.get(project_id=ctx.project_id, config_id=config_id)
        assert row is not None

    return _to_response(row)


@router.delete(
    "/models/{config_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除模型配置",
)
async def delete_model_config(
    config_id: uuid.UUID,
    ctx: TenantCtx,
    pg: TenantPg,
) -> None:
    check_permission(ctx.role, Permission.WRITE)
    async with pg.session() as session:
        repo = LlmModelConfigRepository(session)
        try:
            await repo.delete(project_id=ctx.project_id, config_id=config_id)
        except LlmModelConfigNotFoundError as exc:
            raise NotFoundError(str(exc)) from exc


__all__ = ["router"]
