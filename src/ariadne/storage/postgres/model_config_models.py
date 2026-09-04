"""LLM 模型配置表：llm_model_configs。

用户在设置页自定义的 LLM provider 配置（名称 / model / api_key / base_url）。
project_id FK + RLS 确保多租户隔离。api_key 对称加密存储 —— Loop
Engine 需要明文调用 provider，故不可用不可逆哈希（与 api_keys 表不同）。

加密密钥从 Settings.api.jwt_secret 派生（Fernet），见 repositories/model_configs.py。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ariadne.storage.postgres.models import Base, TimestampMixin, UuidType


class LlmModelConfig(Base, TimestampMixin):
    """用户自定义的 LLM 模型配置。

    一条记录 = 一个可命名的 provider 端点（如"我的 GPT-4o"、"本地 Ollama"）。
    is_default=True 表示该配置为项目默认（项目内至多一条）。
    """

    __tablename__ = "llm_model_configs"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_llm_config_name"),
        Index("ix_llm_configs_project", "project_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    # 用户自取的显示名（"工作用 GPT-4o"），项目内唯一
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # provider: anthropic | openai | openai_compatible
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    # 模型名（用户自填，无枚举限制 —— 支持自定义/网关模型）
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    # 对称加密后的 provider API key（Fernet token，base64）
    api_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # provider 端点；空则用 provider 官方默认
    base_url: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    # 预算降级用的便宜模型（可选）
    degraded_model: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # 是否为项目默认配置。项目内至多一条 true，由 repository 保证
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 显示顺序（用户在设置页可拖拽排序）
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


__all__ = ["LlmModelConfig"]
