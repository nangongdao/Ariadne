"""loop runs and checkpoints

Revision ID: a1b2c3d4e5f6
Revises: 4045e1f7253b
Create Date: 2026-08-26 12:00:00.000000

loop_runs / loop_checkpoints 两张表（docs/08 的 DDL）。
loop_runs 承载 Loop 可变运行态与租约（崩溃接管用）；loop_checkpoints
每轮一条不可变快照，主键 (loop_id, iteration) 保证幂等写。

注：spec_id 暂不加外键约束 —— specs 表在 M3 尚未建，M5 建表后补回。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ariadne.storage.postgres.models

revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "4045e1f7253b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "loop_runs",
        sa.Column("id", ariadne.storage.postgres.models.UuidType(length=36), nullable=False),
        sa.Column("project_id", ariadne.storage.postgres.models.UuidType(length=36), nullable=False),
        sa.Column("spec_id", ariadne.storage.postgres.models.UuidType(length=36), nullable=True),
        sa.Column("mode", sa.String(length=50), nullable=False),
        sa.Column("goal", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cumulative_tokens", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cumulative_cost_usd", sa.Numeric(precision=12, scale=8), nullable=False, server_default="0"),
        sa.Column("final_state", sa.String(length=30), nullable=True),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "state IN ('CREATED','VALIDATE','PLANNING','PRECHECK','EXECUTING','EVALUATING',"
            "'JUDGING','REVISING','HUMAN_PENDING','CONVERGED','REJECTED','BLOCKED',"
            "'BUDGET_EXCEEDED','MAX_ITERATIONS','STALLED','FAILED','CANCELLED')",
            name="valid_state",
        ),
    )
    op.create_index("ix_loop_runs_project_created", "loop_runs", ["project_id", "created_at"])
    # 部分索引（WHERE final_state IS NULL）：活跃任务扫描与租约回收。
    # Postgres 特有，SQLite 回退时 alembic 会忽略 postgresql_where。
    op.create_index(
        "ix_loop_runs_state_active",
        "loop_runs",
        ["state"],
        postgresql_where=sa.text("final_state IS NULL"),
    )
    op.create_index(
        "ix_loop_runs_lease",
        "loop_runs",
        ["lease_expires_at"],
        postgresql_where=sa.text("final_state IS NULL"),
    )

    op.create_table(
        "loop_checkpoints",
        sa.Column("loop_id", ariadne.storage.postgres.models.UuidType(length=36), nullable=False),
        sa.Column("iteration", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=30), nullable=False),
        sa.Column("verdict", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("critique", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=True),
        sa.Column("artifact_refs", sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"), nullable=False, server_default="[]"),
        sa.Column("cumulative_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cumulative_cost_usd", sa.Numeric(precision=12, scale=8), nullable=False),
        sa.Column("output_fp", sa.String(length=64), nullable=False),
        sa.Column("failure_fp", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["loop_id"], ["loop_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("loop_id", "iteration"),
    )
    op.create_index("ix_loop_checkpoints_loop_iter", "loop_checkpoints", ["loop_id", "iteration"])


def downgrade() -> None:
    op.drop_index("ix_loop_checkpoints_loop_iter", table_name="loop_checkpoints")
    op.drop_table("loop_checkpoints")
    op.drop_index("ix_loop_runs_lease", table_name="loop_runs")
    op.drop_index("ix_loop_runs_state_active", table_name="loop_runs")
    op.drop_index("ix_loop_runs_project_created", table_name="loop_runs")
    op.drop_table("loop_runs")
