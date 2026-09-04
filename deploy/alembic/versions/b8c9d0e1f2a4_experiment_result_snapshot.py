"""experiments.result_snapshot column for per-sample compare data

Revision ID: b8c9d0e1f2a4
Revises: b8c9d0e1f2a3
Create Date: 2026-08-30 10:00:00.000000

compare 的置信区间 / churn / flipped_to_fail 都需要逐样本明细，
聚合指标算不出来。此前 save_result 只写聚合，正常完成的实验一律
对比失败（400）；只有手动调 PUT /snapshot 才能补上，且快照被塞进
config 列，会被列表查询一并拉出。

本迁移把快照挪到独立列：
- ORM 侧 deferred，select(Experiment) 不加载，列表查询无回归
- 旧数据的 config["_result_snapshot"] 就地迁移到新列
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8c9d0e1f2a4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "experiments",
        sa.Column(
            "result_snapshot",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )

    bind = op.get_bind()

    # 旧快照就地迁移，然后从 config 里摘掉，避免两处并存后读到过期副本
    if bind.dialect.name == "postgresql":
        op.execute(
            "UPDATE experiments "
            "SET result_snapshot = config->'_result_snapshot' "
            "WHERE config ? '_result_snapshot'"
        )
        op.execute(
            "UPDATE experiments "
            "SET config = config - '_result_snapshot' "
            "WHERE config ? '_result_snapshot'"
        )


def downgrade() -> None:
    bind = op.get_bind()
    # 回滚前把快照塞回 config，否则降级后对比数据丢失
    if bind.dialect.name == "postgresql":
        op.execute(
            "UPDATE experiments "
            "SET config = jsonb_set("
            "    coalesce(config, '{}'::jsonb), "
            "    '{_result_snapshot}', "
            "    result_snapshot"
            ") "
            "WHERE result_snapshot IS NOT NULL"
        )

    op.drop_column("experiments", "result_snapshot")
