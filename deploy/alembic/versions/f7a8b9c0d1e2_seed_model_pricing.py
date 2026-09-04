"""seed model_pricing with built-in default price table

Revision ID: f7a8b9c0d1e2
Revises: d0e1f2a3b4c5
Create Date: 2026-09-02 10:00:00.000000

model_pricing 表在 4045e1f7253b 建了列、在代码里从没被读过 —— 内建默认价
(telemetry/pricing.py 的 _DEFAULT_TABLE) 一直是系统唯一的定价来源，provider
调价后没有任何入口能更新价格。本迁移把默认表播种成行；应用侧 PricingRepository
启动后读到这些行，默认价从此可被 upsert 覆盖。

幂等：每个 (provider, model) 只存在一行 effective_to IS NULL 的开启区间。
全部值都是内置常量，故 SQL 直接内联（离线模式无法渲染 bound params，
会输出 `= NULL` —— 也绕开 fetchone 在离线 mock 下返回 None 的问题）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7a8b9c0d1e2"
down_revision: str | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EPOCH_LITERAL = "'2024-01-01 00:00:00+00:00'"

# 与 telemetry/pricing.py 的 _DEFAULT_TABLE 保持一致（单位：美元/百万 token）
# (provider, model, input, output, cache_read, cache_write, reasoning)
_DEFAULT_PRICES: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("openai", "gpt-4o", "2.50", "10.00", "1.25", "0", "10.00"),
    ("openai", "gpt-4o-mini", "0.15", "0.60", "0.075", "0", "0.60"),
    ("anthropic", "claude-sonnet-5", "3.00", "15.00", "0.30", "3.75", "15.00"),
    ("anthropic", "claude-haiku-4-5", "1.00", "5.00", "0.10", "1.25", "5.00"),
)

_INSERT_SQL = (
    "INSERT INTO model_pricing "
    "(provider, model, input_per_million, output_per_million, "
    " cache_read_per_million, cache_write_per_million, "
    " reasoning_per_million, effective_from, effective_to, "
    " created_at, updated_at) "
    "VALUES ({provider}, {model}, {inp}, {out}, {cr}, {cw}, {r}, "
    f"{_EPOCH_LITERAL}, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
)


def _lit(value: str) -> str:
    """内联字面量：常量值，仅做单引号转义兜底。"""
    return "'" + value.replace("'", "''") + "'"


_ALREADY_SQL = (
    "SELECT 1 FROM model_pricing "
    "WHERE provider = :p AND model = :m AND effective_to IS NULL"
)


def upgrade() -> None:
    bind = op.get_bind()
    # alembic 离线模式（--sql）：as_sql=True，mock 连接无法执行 SELECT，
    # 也无 params 渲染 —— 跳过查重，直接输出 INSERT。
    offline = op.get_context().as_sql
    for provider, model, inp, out, cr, cw, r in _DEFAULT_PRICES:
        if not offline:
            existing = bind.execute(
                sa.text(_ALREADY_SQL), {"p": provider, "m": model}
            ).fetchone()
            if existing:
                continue
        op.execute(
            _INSERT_SQL.format(
                provider=_lit(provider),
                model=_lit(model),
                inp=inp,
                out=out,
                cr=cr,
                cw=cw,
                r=r,
            )
        )


def downgrade() -> None:
    op.execute(f"DELETE FROM model_pricing WHERE effective_from = {_EPOCH_LITERAL}")