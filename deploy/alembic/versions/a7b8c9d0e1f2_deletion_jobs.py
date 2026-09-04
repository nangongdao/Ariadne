"""deletion_jobs table for GDPR cascade delete

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-08-28 16:00:00.000000

M6 生产化（Week 3）：
- deletion_jobs 表：记录 GDPR 删除任务状态
  ClickHouse ALTER TABLE DELETE 是异步 mutation，需跟踪进度
  M6 §4.3：不承诺"立即删除"，最长 24 小时内完成
- RLS 策略：deletion_jobs 是 project-scoped 表
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "deletion_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("subject_id", sa.String(256), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "clickhouse_mutation_id", sa.String(256), nullable=False, server_default=""
        ),
        sa.Column("postgres_deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("clickhouse_deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("s3_deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_index(
        "ix_deletion_jobs_project",
        "deletion_jobs",
        ["project_id"],
    )

    # RLS 策略（仅 Postgres）
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE deletion_jobs ENABLE ROW LEVEL SECURITY")
        op.execute(
            "DROP POLICY IF EXISTS tenant_isolation ON deletion_jobs"
        )
        op.execute(
            "CREATE POLICY tenant_isolation ON deletion_jobs "
            "USING (project_id = current_setting('ariadne.project_id')::uuid)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON deletion_jobs")
        op.execute("ALTER TABLE deletion_jobs DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_deletion_jobs_project", table_name="deletion_jobs")
    op.drop_table("deletion_jobs")
