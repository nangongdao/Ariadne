"""graph_runs 增加租约字段（阶段 2）

Revision ID: h9i0j1k2l3m4
Revises: g8h9i0j1k2l3
Create Date: 2026-09-03 14:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "h9i0j1k2l3m4"
down_revision = "g8h9i0j1k2l3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """添加 worker_id 和 lease_expires_at 字段以支持多 Worker 租约机制。"""
    # 1. 添加租约字段
    op.add_column(
        "graph_runs",
        sa.Column("worker_id", sa.Text(), nullable=True, comment="当前持有租约的 Worker ID"),
    )
    op.add_column(
        "graph_runs",
        sa.Column(
            "lease_expires_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="租约过期时间（用于崩溃接管）",
        ),
    )

    # 2. 添加租约索引（扫描过期租约用）
    op.create_index(
        "ix_graph_runs_lease",
        "graph_runs",
        ["lease_expires_at"],
        postgresql_where=sa.text("state IN ('PENDING', 'RUNNING')"),
    )


def downgrade() -> None:
    """回退租约字段。"""
    op.drop_index("ix_graph_runs_lease", table_name="graph_runs")
    op.drop_column("graph_runs", "lease_expires_at")
    op.drop_column("graph_runs", "worker_id")
