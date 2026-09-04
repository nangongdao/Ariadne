"""依赖注入与认证。

M6 双后端：
- static（默认，测试用）：配置提供的静态 key，返回 settings.api.default_project_id
- db：查 api_keys 表 → Argon2id 验证 → 返回 project_id + 设置租户上下文

require_project -> UUID 签名不变（所有 router 通过 ProjectId 注入）。
新增 require_tenant -> TenantContext 用于需要 role 的场景。
"""

from __future__ import annotations

import contextlib
import hmac
from collections.abc import AsyncIterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, Request

from ariadne.api.errors import UnauthorizedError
from ariadne.auth.key_lookup import resolve_projects_by_prefix
from ariadne.auth.keys import extract_prefix, verify_api_key
from ariadne.auth.rbac import Role
from ariadne.auth.tenant import TenantContext
from ariadne.config import Settings, get_settings
from ariadne.storage.clickhouse import ClickHouseStore
from ariadne.storage.postgres.engine import PostgresStore
from ariadne.storage.postgres.repositories.api_keys import ApiKeyRepository
from ariadne.storage.queue import SpanQueue
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

_BEARER_PREFIX = "Bearer "


def get_app_settings() -> Settings:
    return get_settings()


def _extract_key(
    authorization: str | None, x_ariadne_key: str | None
) -> str:
    """从请求头提取 API Key。"""
    provided = x_ariadne_key or ""
    if not provided and authorization and authorization.startswith(_BEARER_PREFIX):
        provided = authorization.removeprefix(_BEARER_PREFIX)
    return provided


async def require_project(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_ariadne_key: Annotated[str | None, Header()] = None,
) -> UUID:
    """校验 API Key 并返回其所属 project_id。

    支持两种传法：Authorization: Bearer <key> 或 X-Ariadne-Key: <key>。
    static 模式：配置提供的静态 key（M1 兼容）。
    db 模式：查 api_keys 表 + Argon2id 验证。
    """
    settings: Settings = request.app.state.settings
    provided = _extract_key(authorization, x_ariadne_key)

    if not provided:
        raise UnauthorizedError("缺少 API Key（Authorization: Bearer 或 X-Ariadne-Key）")

    if settings.api.auth_backend == "db":
        return await _auth_db(request, provided)

    # static 模式（M1 兼容）
    expected = settings.api.static_api_key.get_secret_value()
    if not hmac.compare_digest(provided, expected):
        raise UnauthorizedError("API Key 无效")
    return settings.api.default_project_id


async def require_tenant(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_ariadne_key: Annotated[str | None, Header()] = None,
) -> TenantContext:
    """校验 API Key 并返回完整租户上下文（含 role）。

    用于需要 role 做 RBAC 权限检查的路由。
    """
    settings: Settings = request.app.state.settings
    provided = _extract_key(authorization, x_ariadne_key)

    if not provided:
        raise UnauthorizedError("缺少 API Key（Authorization: Bearer 或 X-Ariadne-Key）")

    if settings.api.auth_backend == "db":
        return await _auth_db_tenant(request, provided)

    # static 模式：默认 admin 角色
    expected = settings.api.static_api_key.get_secret_value()
    if not hmac.compare_digest(provided, expected):
        raise UnauthorizedError("API Key 无效")
    ctx = TenantContext(project_id=settings.api.default_project_id, role=Role.ADMIN)
    request.state.tenant_context = ctx
    return ctx


async def _touch_last_used(
    pg: PostgresStore, project_id: UUID, key_id: UUID
) -> None:
    """更新 last_used_at。刻意用独立会话。

    并入认证事务的话，这条 UPDATE 一失败事务即 aborted，退出上下文时的
    commit 照样抛 —— suppress(Exception) 包住 UPDATE 也挡不住，已成立的
    认证会变成 500。使用统计不值得这个代价。
    """
    try:
        async with pg.tenant_session(project_id) as session:
            await ApiKeyRepository(session).update_last_used(
                project_id=project_id, key_id=key_id
            )
    except Exception as exc:
        logger.warning(
            "api key last_used_at 更新失败",
            extra={"project_id": str(project_id), "error": str(exc)},
        )


async def _auth_db_tenant(request: Request, provided_key: str) -> TenantContext:
    """DB 后端认证：前缀定位租户 -> RLS 会话内取哈希 -> Argon2id 验证。

    分两步查不是为了绕开 RLS，而是绕不开：策略要求 ariadne.project_id
    已设置，而认证时它还藏在待验证的这把 key 里。第一步只解析候选
    project_id（不含哈希，见 auth.key_lookup），第二步开 tenant_session
    经 RLS 正常取行 —— 哈希始终在 RLS 之后取，策略配错了当场 401 而非
    静默放行。

    遍历候选而非取唯一行：uq_api_key_prefix 是 (project_id, key_prefix)
    复合唯一，前缀只在项目内唯一。前缀 16 字符里仅 7 字符来自随机段
    （约 42 bit），跨项目撞前缀是会发生的，而原先的 scalar_one_or_none()
    一撞就抛 MultipleResultsFound —— 500，且外部可诱发。
    """
    pg: PostgresStore = request.app.state.pg
    key_prefix = extract_prefix(provided_key)

    async with pg.session() as session:
        candidates = await resolve_projects_by_prefix(session, key_prefix)

    for project_id in candidates:
        async with pg.tenant_session(project_id) as session:
            repo = ApiKeyRepository(session)
            row = await repo.get_by_prefix(
                project_id=project_id, key_prefix=key_prefix
            )
            if row is None:
                # 第一步查到了、第二步没查到。两种可能：并发吊销（正常，
                # 单发），或 GUC 与策略对不上（异常，会刷屏）。日志量本身
                # 就是区分二者的信号。
                logger.warning(
                    "api key 前缀已解析但 RLS 会话内取不到行",
                    extra={"project_id": str(project_id)},
                )
                continue

            if not verify_api_key(provided_key, row.key_hash):
                continue

            # 用 repo.is_expired 而非内联比较：SQLite 回的是 naive datetime，
            # 与 datetime.now(UTC) 直接比较抛 TypeError（500，而非预期的 401）。
            # 归一化逻辑只该有一份。
            if await repo.is_expired(row):
                raise UnauthorizedError("API Key 已过期")

            key_id = row.id
            role = row.role

        await _touch_last_used(pg, project_id, key_id)

        ctx = TenantContext(project_id=project_id, role=Role(role))
        request.state.tenant_context = ctx
        return ctx

    raise UnauthorizedError("API Key 无效")


async def _auth_db(request: Request, provided_key: str) -> UUID:
    """DB 后端认证，只取 project_id。"""
    ctx = await _auth_db_tenant(request, provided_key)
    return ctx.project_id


def get_store(request: Request) -> ClickHouseStore:
    store: ClickHouseStore = request.app.state.store
    return store


def get_queue(request: Request) -> SpanQueue:
    queue: SpanQueue = request.app.state.queue
    return queue


def get_pg(request: Request) -> PostgresStore:
    pg: PostgresStore = request.app.state.pg
    return pg


class TenantScopedPg:
    """带租户上下文的 PostgresStore 代理。

    session() 自动注入 project_id → SET LOCAL ariadne.project_id（RLS）。
    连接归还前自动 RESET（见 PostgresStore.tenant_session）。
    """

    def __init__(self, pg: PostgresStore, project_id: UUID) -> None:
        self._pg = pg
        self._project_id = project_id

    @property
    def engine(self) -> Any:
        return self._pg.engine

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        async with self._pg.tenant_session(self._project_id) as session:
            yield session

    async def ping(self) -> bool:
        return await self._pg.ping()

    async def close(self) -> None:
        await self._pg.close()


def get_tenant_pg(
    request: Request,
    ctx: Annotated[TenantContext, Depends(require_tenant)],
) -> TenantScopedPg:
    """返回带 RLS 上下文的 PostgresStore 代理。

    显式 Depends(require_tenant) 而非只读 request.state：后者要求
    require_tenant 先于本依赖解析，而那只由端点参数顺序保证 —— 顺序
    一变就静默退化成 default_project_id（RLS 层面即越权）。
    """
    pg: PostgresStore = request.app.state.pg
    return TenantScopedPg(pg, ctx.project_id)


def get_tenant_context(request: Request) -> TenantContext:
    """从 request.state 获取租户上下文（require_tenant 设置）。"""
    ctx: TenantContext | None = getattr(request.state, "tenant_context", None)
    if ctx is None:
        # require_project 设置了 project_id 但没设 tenant_context（static 模式）
        settings: Settings = request.app.state.settings
        return TenantContext(
            project_id=settings.api.default_project_id, role=Role.ADMIN
        )
    return ctx


ProjectId = Annotated[UUID, Depends(require_project)]
Store = Annotated[ClickHouseStore, Depends(get_store)]
Queue = Annotated[SpanQueue, Depends(get_queue)]
Pg = Annotated[PostgresStore, Depends(get_pg)]
TenantPg = Annotated[TenantScopedPg, Depends(get_tenant_pg)]
AppSettings = Annotated[Settings, Depends(get_app_settings)]
TenantCtx = Annotated[TenantContext, Depends(require_tenant)]
