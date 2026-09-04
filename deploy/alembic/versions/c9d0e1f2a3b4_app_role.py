"""app role: 非 owner 应用角色 ariadne_app

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a4
Create Date: 2026-08-30 10:00:00.000000

M6 生产化：建应用专用的非 owner 角色，让 13 张表上的 tenant_isolation
策略真正生效。

PG 有三条 RLS 绕过路径：superuser、BYPASSRLS、表 owner（未开
FORCE ROW LEVEL SECURITY 时）。应用此前一直用 owner 连接，正踩第三条 ——
策略写了但从不生效。不用 FORCE，那会让迁移自己也被策略挡住。

ariadne_app 只有 DML（SELECT/INSERT/UPDATE/DELETE）。刻意不给：
- SUPERUSER / BYPASSRLS —— 另两条绕过路径
- 任何表的 ownership
- TRUNCATE / REFERENCES / TRIGGER —— 应用代码里没有一处用到
- CREATE ON SCHEMA —— 它建的表它就是 owner，又绕回第三条
- alembic_version 上的任何权限 —— 迁移账本不给应用碰

密码传递：经绑定参数写入 transaction-local GUC，再由 DO 块用 quote_literal
转义拼进 DDL。三个不能在 Python 侧拼 SQL 字符串的理由：
- text() 会把密码里的 :abc 当绑定参数名，直接报 "value is required"
- asyncpg 走 format paramstyle，密码里的 % 会被误解析
- log_statement='ddl' 把 CREATE/ALTER ROLE ... PASSWORD 原文记进日志且不脱敏；
  PL/pgSQL EXECUTE 里的 DDL 不会被单独记录
用 quote_literal 而不是 format('%L', ...)，后者会把 % 带回 SQL 文本。
（log_statement='all' 下 SELECT set_config 仍会入日志，那是该设置的固有代价。）

offline 模式（alembic upgrade --sql）刻意不输出密码 —— literal_binds=True
会把它内联进待审核的 SQL 文件。生成的脚本建 NOLOGIN 角色，
密码由 DBA 另行 ALTER ROLE 设置。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

from ariadne.config import get_settings

revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger(__name__)

# 角色名写死而不读 settings.app_user：downgrade 必须准确知道该删哪个角色，
# 而 alembic 的版本账本不记录当时的环境变量。两者不一致时在 upgrade 里告警。
_APP_ROLE = "ariadne_app"
_PW_GUC = "ariadne.app_password"

# 已存在的角色不用 ALTER 去摘 SUPERUSER/BYPASSRLS：ALTER ROLE ... NOSUPERUSER
# 本身就要 superuser（PG 只看选项在不在，不看值），迁移账号通常没有。
# 宁可报错也不假装修好了。CREATE ROLE 上可以写全 NOxxx —— PG 只在值为真时校验。
_ENSURE_ROLE_SQL = f"""
DO $ariadne_app_role$
DECLARE
    v_pw     text := current_setting('{_PW_GUC}', true);
    v_super  boolean;
    v_bypass boolean;
BEGIN
    SELECT rolsuper, rolbypassrls INTO v_super, v_bypass
    FROM pg_roles WHERE rolname = '{_APP_ROLE}';

    IF NOT FOUND THEN
        IF v_pw IS NULL OR v_pw = '' THEN
            EXECUTE 'CREATE ROLE {_APP_ROLE} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS';
            RAISE WARNING '角色 {_APP_ROLE} 已建但为 NOLOGIN：未提供 ARIADNE_PG_APP_PASSWORD。设置密码前应用连不上，会回退 owner 连接而 RLS 失效';
        ELSE
            EXECUTE 'CREATE ROLE {_APP_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD ' || quote_literal(v_pw);
        END IF;
    ELSIF v_super OR v_bypass THEN
        RAISE EXCEPTION '角色 {_APP_ROLE} 带 SUPERUSER 或 BYPASSRLS，会绕过 RLS。请由 superuser 执行 ALTER ROLE {_APP_ROLE} NOSUPERUSER NOBYPASSRLS 后重跑本迁移';
    ELSIF v_pw IS NOT NULL AND v_pw <> '' THEN
        EXECUTE 'ALTER ROLE {_APP_ROLE} LOGIN PASSWORD ' || quote_literal(v_pw);
    END IF;
END
$ariadne_app_role$
"""

# PG <= 14 里 PUBLIC 默认持有 public schema 的 CREATE 权限。不收回的话
# ariadne_app 能自己建表，而它是自己建的表的 owner —— 又绕回 owner 那条
# RLS 旁路。托管 PG（RDS / CloudSQL）上 public 常属于 bootstrap superuser，
# 收不回来；那种情况降级为告警，不让整条迁移失败。
# 注意这条对整库生效：与其他应用共用同一个库时，请评估后再放行。
_REVOKE_PUBLIC_CREATE_SQL = """
DO $ariadne_revoke_public$
BEGIN
    REVOKE CREATE ON SCHEMA public FROM PUBLIC;
EXCEPTION
    WHEN insufficient_privilege THEN
        RAISE WARNING 'REVOKE CREATE ON SCHEMA public FROM PUBLIC 失败（当前账号不是 public schema 的 owner），托管 PG 上常见。请让 DBA 手动执行，否则应用角色可在 public 建表并成为其 owner，这些表上的 RLS 不生效';
END
$ariadne_revoke_public$
"""

# DROP OWNED BY 必须排在 DROP ROLE 之前：它清掉 pg_shdepend 里指向该角色的
# SHARED_DEPENDENCY_ACL 条目（含 pg_default_acl 中该角色作为 grantee 的行），
# 否则 DROP ROLE 会报"仍有对象依赖于该角色"。所以不必再逐条写
# ALTER DEFAULT PRIVILEGES ... REVOKE ALL。
# 存在性判断放在 SQL 里而不是 Python 里，offline 模式下行为才一致。
_DROP_ROLE_SQL = f"""
DO $ariadne_drop_app_role$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{_APP_ROLE}') THEN
        EXECUTE 'DROP OWNED BY {_APP_ROLE}';
        EXECUTE 'DROP ROLE {_APP_ROLE}';
    END IF;
END
$ariadne_drop_app_role$
"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite（单元测试）没有角色系统，也没有 RLS
        return

    settings = get_settings().postgres
    password = settings.app_password.get_secret_value()
    if password and not context.is_offline_mode():
        # transaction-local GUC：只在本迁移事务内可见，提交即消失。
        # set_config 是普通函数，三个参数都能走绑定参数 —— 密码不进 SQL 文本。
        bind.execute(
            sa.text("SELECT set_config(:guc, :pw, true)"),
            {"guc": _PW_GUC, "pw": password},
        )

    op.execute(_ENSURE_ROLE_SQL)

    # ---- 授权：只给 DML ----
    op.execute(f"GRANT USAGE ON SCHEMA public TO {_APP_ROLE}")
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {_APP_ROLE}"
    )
    # 序列现在可能一张都没有（主键全是 UUID），这条是 no-op。留着是因为将来加一张
    # 自增表时，缺的权限会以运行期 "permission denied for sequence" 的形式很晚才暴露。
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {_APP_ROLE}")

    # 后续迁移新建的表自动带上同样权限，不然每加一张表都要手动补授权。
    # 省略 FOR ROLE 即默认 current_user —— 正是执行迁移的 owner。
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {_APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {_APP_ROLE}"
    )

    # 必须排在上面的 ON ALL TABLES 之后 —— 那条把 alembic_version 一起授了。
    # 应用改得动版本账本，下次 upgrade 就会重放或跳过迁移。
    op.execute(f"REVOKE ALL ON TABLE alembic_version FROM {_APP_ROLE}")

    op.execute(_REVOKE_PUBLIC_CREATE_SQL)

    # 角色建好了不等于应用会用它 —— 配置对不上时这条迁移的全部工作都白做，
    # 且失效方式是静默的（RLS 不报错，只是不生效）。所以在这里就喊出来。
    if not settings.app_user:
        logger.warning(
            "角色 %s 已就绪，但 ARIADNE_PG_APP_USER 未设置：应用仍以 owner 连接，"
            "RLS 对 owner 不生效，等于没启用",
            _APP_ROLE,
        )
    elif settings.app_user != _APP_ROLE:
        logger.warning(
            "ARIADNE_PG_APP_USER=%s 与本迁移创建的角色 %s 不一致："
            "应用连上后不会有任何表权限。请改配置，或手动为前者授同样的权限",
            settings.app_user,
            _APP_ROLE,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    # 不恢复 PUBLIC 的 CREATE ON SCHEMA public：PG 15+ 全新库本来就没有这项，
    # 恢复反而会让库比初装更松。downgrade 在这一点上刻意不对称。
    op.execute(_DROP_ROLE_SQL)
