"""auth tables: api_keys + RLS policies

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-28 10:00:00.000000

M6 生产化（Week 1）：
- api_keys：API Key 表，Argon2id 哈希存储，project_id FK + RLS
- RLS 策略：为所有 project-scoped 表启用行级安全
  策略: USING (project_id = current_setting('ariadne.project_id', true)::uuid)

RLS 是多租户隔离的第二层防线（§3）。应用层 repository 已带 project_id 过滤，
RLS 在应用层漏过滤时兜底 —— 数据库直接拒绝返回跨租户数据。

连接池在每次取用连接时执行 SET LOCAL ariadne.project_id = ...，
连接归还前必须 RESET（见 auth/tenant.py），否则连接复用会导致租户串号。

SQLite 侧跳过 RLS（PG-only 语法），单元测试走应用层隔离。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import ariadne.storage.postgres.models

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 所有 project-scoped 表 —— 需要启用 RLS 并创建策略
# dataset_items 通过 dataset_id 间接关联 project_id，不在 RLS 列表
#
# loop_checkpoints 刻意不在此列表：它的 project_id 列由 f6a7b8c9d0e1 添加。
# PG 在 CREATE POLICY 时就解析 USING 表达式，引用不存在的列立即报错；
# 而 Alembic 单个迁移就是单个事务，报错会把整个 e5f6 回滚掉，
# alembic upgrade head 从此卡在这一步。该表的 ENABLE + POLICY
# 一并交给 f6a7b8c9d0e1 在列加好之后做。
_RLS_TABLES = [
    "datasets",
    "experiments",
    "prompt_versions",
    "judge_calibrations",
    "audit_log",
    "approvals",
    "rule_sets",
    "workflow_graphs",
    "loop_runs",
    "api_keys",
]


def upgrade() -> None:
    # ---- api_keys 表 ----
    op.create_table(
        "api_keys",
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
        sa.Column("key_hash", sa.String(256), nullable=False),
        sa.Column("key_prefix", sa.String(20), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="viewer"),
        sa.Column(
            "scopes",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint("project_id", "key_prefix", name="uq_api_key_prefix"),
    )
    op.create_index("ix_api_keys_prefix", "api_keys", ["key_prefix"])
    op.create_index("ix_api_keys_project_active", "api_keys", ["project_id", "is_active"])

    # ---- RLS 策略（仅 Postgres）----
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in _RLS_TABLES:
            # 启用行级安全
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            # 创建租户隔离策略
            # current_setting(..., true) 在变量未设置时返回 NULL 而非报错
            op.execute(
                f"CREATE POLICY tenant_isolation ON {table} "
                f"USING (project_id = current_setting('ariadne.project_id', true)::uuid)"
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in _RLS_TABLES:
            op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
            op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.drop_index("ix_api_keys_project_active", table_name="api_keys")
    op.drop_index("ix_api_keys_prefix", table_name="api_keys")
    op.drop_table("api_keys")
