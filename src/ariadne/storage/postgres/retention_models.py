"""GDPR 删除任务 Postgres 表。

ClickHouse 的 ALTER TABLE DELETE 是异步 mutation，
deletion_jobs 表记录删除任务状态供进度查询。

M6 §4.3：不承诺"立即删除"，最长 24 小时内完成。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from ariadne.storage.postgres.models import Base, TimestampMixin, UuidType


class DeletionJobRow(Base, TimestampMixin):
    """GDPR 删除任务记录。"""

    __tablename__ = "deletion_jobs"
    __table_args__ = (Index("ix_deletion_jobs_project", "project_id"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), ForeignKey("projects.id"), nullable=False
    )
    # 可选的 subject 级删除（如 user_id），空字符串表示删除整个项目数据
    subject_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    # pending → postgres_done → clickhouse_mutation_submitted → s3_done → completed
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # ClickHouse mutation ID（用于进度查询）
    clickhouse_mutation_id: Mapped[str] = mapped_column(
        String(256), nullable=False, default=""
    )
    postgres_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clickhouse_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    s3_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 认领租约（多副本防双删）。claimed_by = Worker 实例标识，
    # lease_expires_at 过期后可被重新认领 —— 级联删除本身幂等（DELETE WHERE
    # 重跑删 0 行），重放安全。
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 级联开始时间（迁移 a7b8c9d0e1f2 建列，模型此前漏映射 —— 真机验收
    # 的漂移扫描发现。只读使用：查询删除耗时用）
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = ["DeletionJobRow"]
