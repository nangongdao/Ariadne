"""Postgres ORM 模型。

承载"需要事务与外键"的数据（见 docs/08 的分工判据）：项目、数据集、
实验、prompt 版本、计价表。逐样本的 eval 明细仍在 ClickHouse。

M2 阶段的租户模型仍是单 project，但表结构按多租户设计 ——
M6 加 RBAC 与 RLS 时不需要改表，只需加策略。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# JSONB 在 Postgres 上有索引与操作符支持；SQLite 回退到 JSON 便于
# 无容器时跑单元测试（集成测试仍连真实 Postgres）
JsonType = JSON().with_variant(JSONB(), "postgresql")


class UuidType(TypeDecorator[uuid.UUID]):
    """跨方言的 UUID。

    不能用 `PG_UUID().with_variant(String(36), "sqlite")` —— variant 只改
    DDL 类型，不改 Python 侧的参数绑定，SQLite 驱动收到 UUID 对象会直接
    报 "type 'UUID' is not supported"。必须自己做双向转换。
    """

    impl = String(36)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(String(36))

    def process_bind_param(
        self, value: uuid.UUID | str | None, dialect: Dialect
    ) -> Any:
        if value is None:
            return None
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return str(value)

    def process_result_value(
        self, value: Any, dialect: Dialect
    ) -> uuid.UUID | None:
        if value is None:
            return None
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Organization(Base, TimestampMixin):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    plan: Mapped[str] = mapped_column(String(50), nullable=False, default="free")

    projects: Mapped[list[Project]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )


class Project(Base, TimestampMixin):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("org_id", "slug", name="uq_project_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(
        JsonType, nullable=False, default=dict
    )

    organization: Mapped[Organization] = relationship(back_populates="projects")


class Dataset(Base, TimestampMixin):
    """数据集版本。不可变 —— 每次变更自增 version，绝不覆盖。"""

    __tablename__ = "datasets"
    __table_args__ = (
        UniqueConstraint("project_id", "name", "version", name="uq_dataset_version"),
        Index("ix_datasets_project", "project_id", "name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 复现锚点：实验记录里存这个值，任何人都能确认数据集一致
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    items: Mapped[list[DatasetItemRow]] = relationship(
        back_populates="dataset", cascade="all, delete-orphan"
    )


class DatasetItemRow(Base):
    __tablename__ = "dataset_items"
    __table_args__ = (
        UniqueConstraint("dataset_id", "item_id", name="uq_dataset_item"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False
    )
    item_id: Mapped[str] = mapped_column(String(200), nullable=False)
    input: Mapped[str] = mapped_column(Text, nullable=False)
    expected: Mapped[str | None] = mapped_column(Text, nullable=True)
    item_metadata: Mapped[dict[str, Any]] = mapped_column(
        JsonType, nullable=False, default=dict
    )

    dataset: Mapped[Dataset] = relationship(back_populates="items")


class Experiment(Base, TimestampMixin):
    """一次批量实验。

    dataset_ref 存的是 `name@vN#hash` 而非仅 dataset_id：数据集被删除后
    实验记录仍能说明当时用的是什么，这是审计与复现的最低要求。
    """

    __tablename__ = "experiments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','failed','cancelled')",
            name="ck_experiment_status",
        ),
        Index("ix_experiments_project_created", "project_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidType(), ForeignKey("datasets.id", ondelete="SET NULL"), nullable=True
    )
    dataset_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    config_label: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    # 复现所需的完整配置：模型、参数、评估器与权重、阈值
    config: Mapped[dict[str, Any]] = mapped_column(
        JsonType, nullable=False, default=dict
    )
    # Judge 模型列表。版本切换是破坏性变更，分数不可跨版本比较
    judge_models: Mapped[dict[str, Any]] = mapped_column(
        JsonType, nullable=False, default=dict
    )

    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=Decimal("0")
    )
    metrics: Mapped[dict[str, Any]] = mapped_column(
        JsonType, nullable=False, default=dict
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # 逐样本快照。compare 的 CI / churn / flipped_to_fail 都要逐样本，只有聚合算不出。
    # deferred：列表查询一次拉 200 行，带上全文 output 会拖慢每次请求；
    # 只有 get_with_snapshot() 显式 undefer 时才加载。
    result_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JsonType, nullable=True, deferred=True
    )


class PromptVersion(Base, TimestampMixin):
    """Prompt 版本。只做到"可复现实验"所需的最小集（见 docs/01 非目标）。"""

    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("project_id", "name", "version", name="uq_prompt_version"),
        Index("ix_prompts_project_name", "project_id", "name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    template: Mapped[str] = mapped_column(Text, nullable=False)
    variables: Mapped[dict[str, Any]] = mapped_column(
        JsonType, nullable=False, default=dict
    )
    # production / staging 等标签。同一 label 在项目内唯一由应用层保证
    labels: Mapped[dict[str, Any]] = mapped_column(
        JsonType, nullable=False, default=dict
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class JudgeCalibration(Base, TimestampMixin):
    """Judge 与人工标注的一致性记录。

    存在的理由：κ 门禁在配置加载时校验，需要一个持久化的"该 Judge 在该
    维度上的 κ 是多少"。每次重新标注产生一条新记录，不覆盖历史。
    """

    __tablename__ = "judge_calibrations"
    __table_args__ = (
        Index("ix_calibration_lookup", "project_id", "judge_model", "dimension"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    judge_model: Mapped[str] = mapped_column(String(200), nullable=False)
    dimension: Mapped[str] = mapped_column(String(50), nullable=False)
    kappa: Mapped[Decimal] = mapped_column(Numeric(6, 4), nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    weighting: Mapped[str] = mapped_column(String(20), nullable=False, default="linear")
    verdict: Mapped[str] = mapped_column(String(20), nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")


class ModelPricing(Base, TimestampMixin):
    """计价表。带生效时间区间 —— provider 调价后历史记录仍按当时价格算。"""

    __tablename__ = "model_pricing"
    __table_args__ = (
        Index("ix_pricing_lookup", "provider", "model", "effective_from"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    # 单位：美元 / 百万 Token
    input_per_million: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    output_per_million: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    cache_read_per_million: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )
    cache_write_per_million: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )
    reasoning_per_million: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )
    effective_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    effective_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
