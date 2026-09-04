"""llm_model_configs table for user-defined LLM model configuration

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-28 22:00:00.000000

桌面端/自定义模型配置：
- llm_model_configs：用户在设置页自定义的 LLM provider 端点配置
  （名称 / provider / model / api_key / base_url），project_id FK + RLS
- api_key 对称加密存储（Fernet，密钥从 settings.api.jwt_secret 派生）
  —— Loop Engine 需明文调 provider，故不可用不可逆哈希
- provider 白名单含 openai_compatible，覆盖 Ollama / vLLM / OneAPI 等网关

RLS 策略同其他 project-scoped 表，SQLite 侧跳过。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ariadne.storage.postgres.models

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_model_configs",
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
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("api_key_encrypted", sa.Text(), nullable=False, server_default=""),
        sa.Column("base_url", sa.String(500), nullable=False, server_default=""),
        sa.Column(
            "degraded_model", sa.String(200), nullable=False, server_default=""
        ),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "sort_order", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("project_id", "name", name="uq_llm_config_name"),
    )
    op.create_index("ix_llm_configs_project", "llm_model_configs", ["project_id"])

    # RLS 策略（仅 Postgres）
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE llm_model_configs ENABLE ROW LEVEL SECURITY")
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON llm_model_configs")
        op.execute(
            "CREATE POLICY tenant_isolation ON llm_model_configs "
            "USING (project_id = current_setting('ariadne.project_id', true)::uuid)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP POLICY IF EXISTS tenant_isolation ON llm_model_configs")
        op.execute("ALTER TABLE llm_model_configs DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_llm_configs_project", table_name="llm_model_configs")
    op.drop_table("llm_model_configs")
