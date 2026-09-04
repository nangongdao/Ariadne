"""api key lookup: 前缀 -> project_id 的 SECURITY DEFINER 函数

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-08-30 12:00:00.000000

认证的鸡生蛋问题：api_keys 上的 tenant_isolation 策略要求
current_setting('ariadne.project_id') 已设置，但认证时 project_id 恰恰
还不知道 —— 它就藏在待验证的这把 key 里。GUC 未设时 current_setting
返回 NULL，`project_id = NULL` 恒为 NULL，非 owner 角色一行都看不见。
c9d0e1f2a3b4 让应用改用非 owner 角色之后，每个 API Key 请求都会 401。

本函数是那唯一一处提权：以 owner 身份（owner 天然绕过 RLS，未开 FORCE）
按前缀跨租户查出 project_id 列表。刻意只回 project_id，不回 key_hash ——
哈希仍要走 tenant_session + RLS 正常取。这样做的两个理由：
- 提权面压到最小：泄露的信息只是"某前缀属于哪个项目"
- RLS 留在认证主路径上：配错了当场 401，而不是线上静默失效

返回 SETOF 而非单值：uq_api_key_prefix 是 (project_id, key_prefix) 的
复合唯一，前缀只在项目内唯一。前缀取明文前 16 字符，其中仅 7 字符来自
随机段（约 42 bit），按生日界 ~2^21 把 key 就可能撞上 —— 不是不会发生。

search_path 固定为 pg_catalog, pg_temp 且表名全限定：SECURITY DEFINER
函数若继承调用方 search_path，调用方可建同名 public.api_keys 或临时表
把函数骗去读伪造数据。这是该类函数的标准加固项，非可选。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "ariadne_app"
_FN = "public.ariadne_api_key_projects"

# STABLE：同一语句内多次调用结果一致，可走索引扫描（ix_api_keys_prefix）。
# 不用 IMMUTABLE —— 它读表，表会变。
# 参数名带 p_ 前缀，避免与 api_keys 的列名在函数体里同名遮蔽。
_CREATE_FN_SQL = f"""
CREATE OR REPLACE FUNCTION {_FN}(p_key_prefix text)
RETURNS SETOF uuid
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $ariadne_key_projects$
    SELECT project_id
    FROM public.api_keys
    WHERE key_prefix = p_key_prefix
      AND is_active
$ariadne_key_projects$
"""

# 新建函数默认 EXECUTE 授予 PUBLIC —— 不收回等于库里任何角色都能拿它
# 做前缀存在性探测。先 REVOKE 再按需 GRANT。
_GRANT_FN_SQL = f"""
REVOKE ALL ON FUNCTION {_FN}(text) FROM PUBLIC
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite（单元测试）没有 RLS，认证直接按前缀查表即可
        return

    op.execute(_CREATE_FN_SQL)
    op.execute(_GRANT_FN_SQL)
    op.execute(f"GRANT EXECUTE ON FUNCTION {_FN}(text) TO {_APP_ROLE}")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # 必须带参数签名：函数可重载，不带签名在有同名重载时会报 ambiguous
    op.execute(f"DROP FUNCTION IF EXISTS {_FN}(text)")
