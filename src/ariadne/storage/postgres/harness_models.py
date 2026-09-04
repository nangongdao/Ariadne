"""Harness 相关 Postgres 表：audit_log、approvals、rule_sets。

见 docs/08 的 DDL。

- audit_log：append-only 审计日志，REVOKE UPDATE/DELETE 保证不可篡改（验收项 12）
- approvals：HITL 审批记录，关联 loop_id
- rule_sets：项目级规则集（JSONB 存储），版本化
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ariadne.storage.postgres.models import Base, TimestampMixin, UuidType

JsonType = JSON().with_variant(JSONB(), "postgresql")

# 审批状态集合
_APPROVAL_STATUSES = "'pending','approved','rejected','expired'"


class AuditLogRow(Base):
    """审计日志表 —— append-only。

    验收项 12：不可篡改。迁移脚本通过 REVOKE UPDATE, DELETE 保证 DB 侧
    也不可修改。记录每次 Harness 规则求值的完整决策链（全部命中 + 胜出者）。
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_project_created", "project_id", "created_at"),
        Index("ix_audit_log_loop", "loop_id"),
        Index("ix_audit_log_hook", "hook"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    loop_id: Mapped[uuid.UUID | None] = mapped_column(
        UuidType(), nullable=True
    )
    hook: Mapped[str] = mapped_column(String(30), nullable=False)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    rule_hits: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, nullable=False)
    winning_hit: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    context_snapshot: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ApprovalRow(Base, TimestampMixin):
    """HITL 审批记录。关联 loop_id，有过期时间。"""

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({_APPROVAL_STATUSES})", name="valid_approval_status"
        ),
        Index("ix_approvals_loop", "loop_id"),
        Index("ix_approvals_status", "status"),
        Index("ix_approvals_expires", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    loop_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), ForeignKey("loop_runs.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # 审批上下文（触发审批的规则、上下文快照）
    context: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    # 审批人（批准/拒绝时填写）
    reviewer: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # 审批意见
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RuleSetRow(Base, TimestampMixin):
    """项目级规则集 —— JSONB 存储，版本化。

    规则集是 Harness 的配置：一组 Rule 的 JSON 表示。版本号每次更新递增。
    项目可以有多个规则集（开发/生产环境），但只有一个 active 版本。
    """

    __tablename__ = "rule_sets"
    __table_args__ = (
        Index("ix_rule_sets_project", "project_id"),
        Index("ix_rule_sets_project_version", "project_id", "version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UuidType(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rules: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        nullable=False, default=True, server_default=text("true")
    )


__all__ = [
    "ApprovalRow",
    "AuditLogRow",
    "RuleSetRow",
]
