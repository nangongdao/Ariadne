"""认证相关 Postgres 表：api_keys。

M6 多租户 RBAC 的数据层。API Key 用 Argon2id 哈希存储，
只保留前缀用于列表展示和索引查找。

表结构按多租户设计 —— project_id FK + RLS 策略确保跨租户隔离。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ariadne.storage.postgres.models import Base, TimestampMixin, UuidType

JsonType = JSON().with_variant(JSONB(), "postgresql")


class ApiKey(Base, TimestampMixin):
    """API Key 表。

    Argon2id 哈希存储 —— 明文仅在创建时返回一次。
    key_prefix 是明文前 16 字符，用于列表展示和索引查找
    （哈希不可逆查，所以需要一个可查的前缀字段）。
    """

    __tablename__ = "api_keys"
    __table_args__ = (
        UniqueConstraint("project_id", "key_prefix", name="uq_api_key_prefix"),
        Index("ix_api_keys_prefix", "key_prefix"),
        Index("ix_api_keys_project_active", "project_id", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # Argon2id 哈希（含盐 + 参数，自描述格式）
    key_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    # 明文前缀，用于展示和索引查找。格式: ak_live_abc12345
    key_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # 角色：admin / approver / developer / viewer / billing
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="viewer")
    # 权限范围列表（JSONB），空表示继承角色全部权限
    scopes: Mapped[dict[str, Any]] = mapped_column(
        JsonType, nullable=False, default=dict
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
