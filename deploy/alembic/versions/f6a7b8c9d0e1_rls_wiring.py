"""loop_checkpoints project_id + RLS wiring

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-08-28 14:00:00.000000

M6 生产化（Week 2）：
- loop_checkpoints 表添加 project_id 列（从 loop_runs.project_id 回填）
- 本表的 ENABLE ROW LEVEL SECURITY + tenant_isolation 策略都在这里做，
  不在 e5f6a7b8c9d0 —— 那边建策略时列还不存在，整个迁移会回滚

SQLite 侧跳过 RLS（PG-only 语法）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ariadne.storage.postgres.models

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- loop_checkpoints 添加 project_id ----
    op.add_column(
        "loop_checkpoints",
        sa.Column(
            "project_id",
            ariadne.storage.postgres.models.UuidType(length=36),
            nullable=True,  # 先加为 nullable，回填后再改 NOT NULL
        ),
    )

    # 回填：从 loop_runs.project_id 取值
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            UPDATE loop_checkpoints
            SET project_id = lr.project_id
            FROM loop_runs lr
            WHERE loop_checkpoints.loop_id = lr.id
            """
        )
    else:
        # SQLite
        op.execute(
            """
            UPDATE loop_checkpoints
            SET project_id = (
                SELECT lr.project_id FROM loop_runs lr
                WHERE lr.id = loop_checkpoints.loop_id
            )
            """
        )

    # 改为 NOT NULL（回填后）
    op.alter_column("loop_checkpoints", "project_id", nullable=False)

    # FK + 索引
    op.create_foreign_key(
        "fk_loop_checkpoints_project",
        "loop_checkpoints",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_loop_checkpoints_project",
        "loop_checkpoints",
        ["project_id"],
    )

    # ---- loop_checkpoints 启用 RLS + 建策略（幂等）----
    # ENABLE 也在这里做，不在 e5f6a7b8c9d0：那边建策略时 project_id 列还不存在，
    # 而 PG 在 CREATE POLICY 时即解析 USING 表达式，报错会连带回滚同一事务里的
    # ENABLE。所以对本表而言 ENABLE 从未生效过，必须在此补上。
    # ENABLE 与 DROP POLICY IF EXISTS + CREATE 都可重复执行，重跑安全。
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE loop_checkpoints ENABLE ROW LEVEL SECURITY")
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON loop_checkpoints")
        op.execute(
            "CREATE POLICY tenant_isolation ON loop_checkpoints "
            "USING (project_id = current_setting('ariadne.project_id', true)::uuid)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON loop_checkpoints")
        # e5f6a7b8c9d0 的 downgrade 已不管这张表，这里不 DISABLE 就会留下
        # "RLS 开着但无任何策略" —— 非 owner 角色对该表零可见行
        op.execute("ALTER TABLE loop_checkpoints DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_loop_checkpoints_project", table_name="loop_checkpoints")
    if bind.dialect.name == "postgresql":
        op.drop_constraint(
            "fk_loop_checkpoints_project", "loop_checkpoints", type_="foreignkey"
        )
    op.drop_column("loop_checkpoints", "project_id")
