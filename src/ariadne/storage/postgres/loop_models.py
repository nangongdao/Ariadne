"""Loop 相关 Postgres 表：loop_runs 与 loop_checkpoints。

见 docs/08 的 DDL。loop_runs 承载 Loop 的可变运行态（状态、租约），
loop_checkpoints 每轮一条不可变快照（崩溃恢复用）。

与 datasets/experiments 同模块化：表结构按多租户设计（project_id 外键），
M6 加 RLS 时不需要改表。检查点表的主键是 (loop_id, iteration)，保证
幂等写 —— Worker 接管后重写同一轮检查点不会产生两条。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ariadne.storage.postgres.models import Base, TimestampMixin, UuidType

# JSONB 在 Postgres 上有索引与操作符支持；SQLite 回退到 JSON 便于无容器跑单测。
# 与 models.py 同定义，独立一份避免跨模块耦合。
JsonType = JSON().with_variant(JSONB(), "postgresql")

# 17 个状态的全集，用于 CHECK 约束。与 state_machine.LoopState 保持一致——
# 刻意写成字面量而非从 LoopState 动态生成：DDL 约束在数据库层，与 Python
# 枚举解耦，避免改枚举时遗漏迁移。
_LOOP_STATES = (
    "'CREATED','VALIDATE','PLANNING','PRECHECK','EXECUTING','EVALUATING',"
    "'JUDGING','REVISING','HUMAN_PENDING','CONVERGED','REJECTED','BLOCKED',"
    "'BUDGET_EXCEEDED','MAX_ITERATIONS','STALLED','FAILED','CANCELLED'"
)


class LoopRun(Base, TimestampMixin):
    """Loop 运行实例。承载可变运行态与租约（崩溃接管用）。"""

    __tablename__ = "loop_runs"
    __table_args__ = (
        CheckConstraint("state IN (" + _LOOP_STATES + ")", name="valid_state"),
        Index("ix_loop_runs_project_created", "project_id", "created_at"),
        # 活跃任务扫描与租约回收（docs/08：WHERE final_state IS NULL）
        Index(
            "ix_loop_runs_state_active",
            "state",
            postgresql_where=text("final_state IS NULL"),
        ),
        Index(
            "ix_loop_runs_lease",
            "lease_expires_at",
            postgresql_where=text("final_state IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    spec_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidType(), nullable=True
    )
    # 注：spec_id 引用 specs 表，但 specs 表在 M3 尚未建。
    # M5（编排）会建 specs 表并补回 ForeignKey 约束。此处保留字段避免后续迁移。
    mode: Mapped[str] = mapped_column(String(50), nullable=False)
    # Goal 的序列化（task + assertions + budget + mode）
    goal: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cumulative_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cumulative_cost_usd: Mapped[float] = mapped_column(
        Numeric(12, 8), nullable=False, default=0
    )
    final_state: Mapped[str | None] = mapped_column(String(30), nullable=True)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    worker_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LoopCheckpointRow(Base):
    """检查点：每轮一条不可变快照。

    主键 (loop_id, iteration) 保证幂等写。Worker 接管后重写同一轮检查点
    覆盖而非新增 —— 恢复语义要求"读到什么就是什么"。
    """

    __tablename__ = "loop_checkpoints"
    __table_args__ = (
        # 时间序列扫描：查最新检查点时按 loop_id + iteration 倒序
        Index("ix_loop_checkpoints_loop_iter", "loop_id", "iteration"),
        Index("ix_loop_checkpoints_project", "project_id"),
    )

    loop_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(),
        ForeignKey("loop_runs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    iteration: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    verdict: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    critique: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    artifact_refs: Mapped[list[str]] = mapped_column(JsonType, nullable=False, default=list)
    cumulative_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cumulative_cost_usd: Mapped[float] = mapped_column(
        Numeric(12, 8), nullable=False
    )
    output_fp: Mapped[str] = mapped_column(String(64), nullable=False)
    failure_fp: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = [
    "LoopCheckpointRow",
    "LoopRun",
]
