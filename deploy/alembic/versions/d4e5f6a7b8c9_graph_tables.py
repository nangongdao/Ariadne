"""graph tables: workflow_graphs

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-27 18:00:00.000000

M5 编排与可视化：
- workflow_graphs：项目级 DAG 图定义，JSONB 存储，版本化
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ariadne.storage.postgres.models

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workflow_graphs",
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
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "graph",
            sa.JSON().with_variant(
                sa.dialects.postgresql.JSONB(), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "validation_errors",
            sa.JSON().with_variant(
                sa.dialects.postgresql.JSONB(), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
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
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_graphs_project", "workflow_graphs", ["project_id"]
    )
    op.create_index(
        "ix_workflow_graphs_project_name",
        "workflow_graphs",
        ["project_id", "name"],
    )


def downgrade() -> None:
    op.drop_table("workflow_graphs")
