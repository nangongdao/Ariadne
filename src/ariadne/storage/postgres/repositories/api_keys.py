"""API Key 仓储。

所有方法强制带 project_id（多租户隔离的应用层防线）。
RLS 策略在 Postgres 侧作为兜底（M6 §3）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ariadne.auth.keys import extract_prefix, generate_api_key, hash_api_key
from ariadne.storage.postgres.auth_models import ApiKey


class ApiKeyNotFoundError(LookupError):
    pass


class ApiKeyRepository:
    """API Key 读写。所有方法强制带 project_id（多租户隔离的应用层防线）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_prefix(
        self, *, project_id: uuid.UUID, key_prefix: str
    ) -> ApiKey | None:
        """按前缀查找 key —— 认证两步流程的第二步。

        要求 project_id 已知，所以认证时必须先经
        auth.key_lookup.resolve_projects_by_prefix 解析出候选租户。
        本方法应在 tenant_session 内调用，让 RLS 参与过滤。
        """
        result = await self._session.execute(
            select(ApiKey).where(
                ApiKey.project_id == project_id,
                ApiKey.key_prefix == key_prefix,
                ApiKey.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id(
        self, *, project_id: uuid.UUID, key_id: uuid.UUID
    ) -> ApiKey | None:
        result = await self._session.execute(
            select(ApiKey).where(
                ApiKey.project_id == project_id,
                ApiKey.id == key_id,
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        project_id: uuid.UUID,
        name: str,
        role: str,
        scopes: dict[str, object] | None = None,
        expires_at: datetime | None = None,
    ) -> tuple[uuid.UUID, str]:
        """创建 API Key。返回 (key_id, 明文 key)。

        明文 key 仅在创建时返回一次。之后只存 Argon2id 哈希。
        """
        plain_key = generate_api_key()
        key_prefix = extract_prefix(plain_key)
        key_hash = hash_api_key(plain_key)

        row = ApiKey(
            id=uuid.uuid4(),
            project_id=project_id,
            key_hash=key_hash,
            key_prefix=key_prefix,
            name=name,
            role=role,
            scopes=dict(scopes) if scopes else {},
            is_active=True,
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row.id, plain_key

    async def list(self, *, project_id: uuid.UUID) -> list[ApiKey]:
        """列出项目的所有 key（不含哈希，调用方不应暴露 key_hash）。"""
        result = await self._session.execute(
            select(ApiKey)
            .where(ApiKey.project_id == project_id)
            .order_by(ApiKey.created_at.desc())
        )
        return list(result.scalars().all())

    async def revoke(self, *, project_id: uuid.UUID, key_id: uuid.UUID) -> None:
        """吊销 key（软删除：标记 is_active=False）。"""
        result = await self._session.execute(
            update(ApiKey)
            .where(
                ApiKey.project_id == project_id,
                ApiKey.id == key_id,
            )
            .values(is_active=False)
            .returning(ApiKey.id)
        )
        if result.scalar_one_or_none() is None:
            raise ApiKeyNotFoundError(f"API Key {key_id} 不存在")

    async def update_last_used(self, *, project_id: uuid.UUID, key_id: uuid.UUID) -> None:
        """更新 last_used_at 时间戳。"""
        await self._session.execute(
            update(ApiKey)
            .where(
                ApiKey.project_id == project_id,
                ApiKey.id == key_id,
            )
            .values(last_used_at=datetime.now(UTC))
        )

    async def is_expired(self, key: ApiKey) -> bool:
        """检查 key 是否已过期。"""
        if key.expires_at is None:
            return False
        expires_at = key.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return datetime.now(UTC) > expires_at


__all__ = ["ApiKeyNotFoundError", "ApiKeyRepository"]
