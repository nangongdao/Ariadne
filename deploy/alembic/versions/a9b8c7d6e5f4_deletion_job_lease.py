"""deletion_jobs 加认领租约字段

Revision ID: a9b8c7d6e5f4
Revises: f7a8b9c0d1e2
Create Date: 2026-09-02

多副本部署防双删：RetentionWorker 此前按状态裸查 + 无条件更新，两个副本
会同时认领同一个 pending 任务，重复执行级联删除与 ClickHouse mutation。
claimed_by + lease_expires_at 由"FOR UPDATE SKIP LOCKED + 条件 UPDATE"的
认领语义使用（见 worker/retention_worker.py），过期租约可被重新认领。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9b8c7d6e5f4"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deletion_jobs",
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "deletion_jobs",
        sa.Column(
            "lease_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("deletion_jobs", "lease_expires_at")
    op.drop_column("deletion_jobs", "claimed_by")
