"""harness tables: audit_log, approvals, rule_sets

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-27 10:00:00.000000

三张 M4 Harness 表：
- audit_log：append-only 审计日志，REVOKE UPDATE/DELETE 保证不可篡改（验收项 12）
- approvals：HITL 审批记录，关联 loop_id
- rule_sets：项目级规则集，JSONB 存储，版本化
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ariadne.storage.postgres.models

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- audit_log（append-only，无 updated_at）----
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            ariadne.storage.postgres.models.UuidType(length=36),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            ariadne.storage.postgres.models.UuidType(length=36),
            nullable=False,
        ),
        sa.Column(
            "loop_id",
            ariadne.storage.postgres.models.UuidType(length=36),
            nullable=True,
        ),
        sa.Column("hook", sa.String(length=30), nullable=False),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column(
            "rule_hits",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "winning_hit",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "context_snapshot",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_project_created", "audit_log", ["project_id", "created_at"])
    op.create_index("ix_audit_log_loop", "audit_log", ["loop_id"])
    op.create_index("ix_audit_log_hook", "audit_log", ["hook"])

    # ---- approvals ----
    op.create_table(
        "approvals",
        sa.Column(
            "id",
            ariadne.storage.postgres.models.UuidType(length=36),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            ariadne.storage.postgres.models.UuidType(length=36),
            nullable=False,
        ),
        sa.Column(
            "loop_id",
            ariadne.storage.postgres.models.UuidType(length=36),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column(
            "context",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("reviewer", sa.String(length=100), nullable=True),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["loop_id"], ["loop_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','expired')",
            name="valid_approval_status",
        ),
    )
    op.create_index("ix_approvals_loop", "approvals", ["loop_id"])
    op.create_index("ix_approvals_status", "approvals", ["status"])
    op.create_index("ix_approvals_expires", "approvals", ["expires_at"])

    # ---- rule_sets ----
    op.create_table(
        "rule_sets",
        sa.Column(
            "id",
            ariadne.storage.postgres.models.UuidType(length=36),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            ariadne.storage.postgres.models.UuidType(length=36),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "rules",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rule_sets_project", "rule_sets", ["project_id"])
    op.create_index("ix_rule_sets_project_version", "rule_sets", ["project_id", "version"])

    # ---- 验收项 12：audit_log 不可篡改（Postgres 侧 REVOKE）----
    op.execute("REVOKE UPDATE, DELETE ON audit_log FROM PUBLIC")
    # 如果有 app_role，也 REVOKE（按需取消注释）
    # op.execute("REVOKE UPDATE, DELETE ON audit_log FROM app_role")


def downgrade() -> None:
    op.drop_table("rule_sets")
    op.drop_table("approvals")
    op.drop_table("audit_log")
