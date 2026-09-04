"""graph_runs 增加节点级 checkpoint 载荷并合并迁移分支。

Revision ID: j2k3l4m5n6o7
Revises: c1d2e3f4a5b6, i1j2k3l4m5n6
Create Date: 2026-09-04

此前 Graph 迁移和 retention/RLS 迁移从 f7a8b9c0d1e2 分叉，形成两个
Alembic head。本迁移在增加 checkpoint JSONB 列的同时合并两条分支，恢复
单一 head。checkpoint 保存已完成节点输出，避免 Worker 恢复时只有状态、
没有数据流。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "j2k3l4m5n6o7"
down_revision: tuple[str, str] = ("c1d2e3f4a5b6", "i1j2k3l4m5n6")
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def upgrade() -> None:
    """增加节点级恢复载荷。"""
    op.add_column(
        "graph_runs",
        sa.Column(
            "checkpoint",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """移除节点级恢复载荷。"""
    op.drop_column("graph_runs", "checkpoint")
