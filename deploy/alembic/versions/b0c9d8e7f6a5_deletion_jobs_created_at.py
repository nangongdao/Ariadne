"""deletion_jobs 补 created_at 列

Revision ID: b0c9d8e7f6a5
Revises: a9b8c7d6e5f4
Create Date: 2026-09-02

真机验收发现的模型/DDL 漂移：模型 DeletionJobRow 继承 TimestampMixin
（created_at + updated_at），但迁移 a7b8c9d0e1f2 手写建表时漏了
created_at。RetentionWorker 的认领查询按 created_at 排序（FIFO），
在真实库上直接报 UndefinedColumnError —— SQLite 测试按模型 create_all
建表，永远发现不了这类漂移（R11 的新实例）。

started_at 是库里有而模型漏映射的列（本轮已在模型补上映射，不需要
DDL 变更）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b0c9d8e7f6a5"
down_revision: str | None = "a9b8c7d6e5f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deletion_jobs",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_column("deletion_jobs", "created_at")
