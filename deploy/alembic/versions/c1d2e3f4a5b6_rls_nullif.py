"""RLS 策略空串加固：NULLIF 包裹 current_setting

Revision ID: c1d2e3f4a5b6
Revises: b0c9d8e7f6a5
Create Date: 2026-09-02

真机验收发现的连接池污染形态：PG 自定义 GUC 一旦在连接上设置过就无法
取消定义（RESET 只会归成空串 ''），tenant_session 归还连接后，池化连接
带着 ariadne.project_id = ''。此后任何无租户上下文的查询在该连接上，
RLS 策略的 current_setting(..., true)::uuid 变成 ''::uuid →
InvalidTextRepresentationError。实测序列：

  [clean-session] current_setting(..., true) -> None
  [after-tenant-session] current_setting(..., true) -> ''   ← 污染
  [second-plain-session] current_setting(..., true) -> ''

修复：全部 tenant_isolation 策略改为
  project_id = NULLIF(current_setting('ariadne.project_id', true), '')::uuid
空串与未设置同等对待 → NULL → 零行可见。附带统一了 deletion_jobs 旧策略
缺 missing_ok 的差异（未设变量从报错改为零行，对 Worker 的补偿扫描语义
更正确）。
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b0c9d8e7f6a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 e5f6a7b8c9d0 / f6a7b8c9d0e1 / a7b8c9d0e1f2 / b8c9d0e1f2a3 四个迁移
# 建过 tenant_isolation 策略的表的并集。
_RLS_TABLES = (
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
    "loop_checkpoints",
    "deletion_jobs",
    "llm_model_configs",
)

_POLICY = (
    "CREATE POLICY tenant_isolation ON {table} "
    "USING (project_id = NULLIF(current_setting('ariadne.project_id', true), '')::uuid)"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in _RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(_POLICY.format(table=table))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    # 回退恢复旧表达式（loop_runs / api_keys 等 10 张原本就是 missing_ok 版；
    # deletion_jobs 恢复其无 missing_ok 的原始形态）
    _legacy_missing_ok = (
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
        "loop_checkpoints",
        "llm_model_configs",
    )
    for table in _RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        if table in _legacy_missing_ok:
            op.execute(
                f"CREATE POLICY tenant_isolation ON {table} "
                "USING (project_id = current_setting('ariadne.project_id', true)::uuid)"
            )
        else:
            op.execute(
                f"CREATE POLICY tenant_isolation ON {table} "
                "USING (project_id = current_setting('ariadne.project_id')::uuid)"
            )
