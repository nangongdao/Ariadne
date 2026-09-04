"""add error column to loop_runs

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-26 15:30:00.000000

loop_runs.error：终态时的错误描述（如"执行失败"）。Worker/API 用到，
错误信息要为前端展示而记录。默认空串，不回滚丢失信息。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "loop_runs",
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("loop_runs", "error")