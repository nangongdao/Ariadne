"""Postgres 引擎与会话管理。

用 async SQLAlchemy：API 层是 async，同步驱动会在事件循环里阻塞。

M6 租户上下文：session() 可选传入 project_id，PG 侧 SET LOCAL
ariadne.project_id（RLS 策略依赖此变量），连接归还前 RESET。
SQLite 侧 SET LOCAL / RESET 是 no-op。

**连接归还前必须重置变量** —— 连接池复用会导致租户串号，这是 RLS
最常见的实现错误。接口已按"每次取会话都显式传 project_id"设计。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from functools import lru_cache
from uuid import UUID

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ariadne.config import PostgresSettings, get_settings
from ariadne.storage.postgres.models import Base
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)


def build_engine(settings: PostgresSettings) -> AsyncEngine:
    """应用引擎 —— 用 app_dsn（非 owner），否则 RLS 会被 owner 静默绕过。

    迁移走的是 deploy/alembic/env.py 里的 dsn()（owner，有 DDL 权限）。
    """
    return create_async_engine(
        settings.app_dsn(),
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_pre_ping=True,  # 长连接被中间件掐断后自动重连
        echo=settings.echo_sql,
    )


class PostgresStore:
    """连接与会话工厂。"""

    def __init__(self, settings: PostgresSettings) -> None:
        self._settings = settings
        self._engine = build_engine(settings)
        self._sessionmaker = async_sessionmaker(
            self._engine,
            expire_on_commit=False,  # 提交后仍可读对象属性，避免意外的懒加载查询
            class_=AsyncSession,
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """带自动提交/回滚的会话。

        异常时回滚而非留下半完成的事务 —— 实验状态流转对一致性敏感。
        """
        async with self._sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def tenant_session(self, project_id: UUID) -> AsyncIterator[AsyncSession]:
        """带租户上下文的会话。

        PG 侧 SET LOCAL ariadne.project_id（RLS 策略依赖），
        连接归还前 RESET 防止租户串号。SQLite 侧 no-op。
        """
        from ariadne.auth.tenant import reset_tenant_context, set_tenant_context

        async with self._sessionmaker() as session:
            try:
                await set_tenant_context(session, project_id)
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                with suppress(Exception):
                    await reset_tenant_context(session)

    async def ping(self) -> bool:
        from sqlalchemy import text

        try:
            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            logger.warning("postgres ping failed", extra={"error": str(exc)})
            return False

    async def create_all(self) -> None:
        """建表。仅用于测试与本地开发 —— 生产环境走 Alembic 迁移。

        理由：create_all 无版本记录、无回滚路径，线上用它会导致
        "schema 是怎么变成现在这样的"无从追溯。
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("postgres tables created (dev only, use alembic in prod)")

    async def drop_all(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def close(self) -> None:
        await self._engine.dispose()


@lru_cache(maxsize=1)
def get_store() -> PostgresStore:
    return PostgresStore(get_settings().postgres)
