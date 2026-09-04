"""RLS 生效性自检。

存在理由：RLS 失效是静默的。三条路径都不会报错，只会让租户隔离消失：

1. 应用连着 owner 角色 —— owner 默认绕过自己表上的策略。而
   PostgresSettings 的 owner 默认值 (ariadne/ariadne) 与 chart 默认 owner
   相同，所以 app_user 留空时 app_dsn() 回落 owner 并**连接成功**。
2. 迁移没跑到 f6a7b8c9d0e1 —— 表建好了，策略没建，查询照常返回全部行。
3. 角色带 BYPASSRLS 或 SUPERUSER —— 策略在，但对这个角色不生效。

三者的共同表现是"功能一切正常"，而不是任何形式的错误。所以在启动时
主动问 PG 一句 row_security_active()：它回答的是"对当前角色，这张表的
RLS 是否真的在生效"，正是上面三条都会让它变成 false 的那个量。

刻意不 hard-fail：迁移只跑了一半的库不该让整个部署起不来，
运维需要能进去查。但日志必须点明"哪张表 + 为什么 + 怎么修"。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ariadne.storage.postgres.engine import PostgresStore
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

# 用 pg_attribute 而非 information_schema.columns：后者只显示当前角色有
# 权限的列，权限不足时会**少报表**，让自检变成空过。系统目录没这层过滤。
_TABLE_QUERY = text("""
SELECT
    c.relname AS table_name,
    c.relrowsecurity AS rls_enabled,
    row_security_active(c.oid) AS rls_active,
    c.relowner = (SELECT oid FROM pg_roles WHERE rolname = current_user) AS is_owner,
    EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid) AS has_policy
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND n.nspname = 'public'
  AND EXISTS (
      SELECT 1 FROM pg_attribute a
      WHERE a.attrelid = c.oid
        AND a.attname = 'project_id'
        AND a.attnum > 0
        AND NOT a.attisdropped
  )
ORDER BY c.relname
""")

_ROLE_QUERY = text("""
SELECT current_user AS role, rolsuper, rolbypassrls
FROM pg_roles WHERE rolname = current_user
""")

_REMEDY_APP_USER = (
    "把 ARIADNE_PG_APP_USER / ARIADNE_PG_APP_PASSWORD 指向非 owner 角色"
    "（ariadne_app，由迁移 c9d0e1f2a3b4 创建）"
)
_REMEDY_MIGRATE = "跑 ariadne-migrate 把迁移推到 head"


@dataclass(frozen=True)
class TableStatus:
    """单表的 RLS 实际状态。"""

    table: str
    rls_enabled: bool
    rls_active: bool
    is_owner: bool
    has_policy: bool


@dataclass(frozen=True)
class RlsReport:
    """自检结果。tables 为空本身就是一种故障，见 problems。"""

    role: str
    is_superuser: bool
    bypasses_rls: bool
    tables: tuple[TableStatus, ...]

    @property
    def inactive(self) -> tuple[TableStatus, ...]:
        return tuple(t for t in self.tables if not t.rls_active)

    @property
    def ok(self) -> bool:
        return not self.problems()

    def problems(self) -> list[str]:
        """按根因归并的问题列表 —— 空列表表示 RLS 确实在生效。"""
        # 空清单不能算通过：all()/any() 对空集恒真，正是这类自检最容易空过的地方
        if not self.tables:
            return [
                f"没有找到任何带 project_id 列的表 —— 库是空的或迁移没跑。{_REMEDY_MIGRATE}"
            ]

        problems: list[str] = []
        if self.is_superuser:
            problems.append(
                f"当前角色 {self.role} 是 superuser，RLS 对它整体不生效。{_REMEDY_APP_USER}"
            )
        if self.bypasses_rls:
            problems.append(
                f"当前角色 {self.role} 带 BYPASSRLS 属性，策略对它不生效。{_REMEDY_APP_USER}"
            )

        owned = [t.table for t in self.inactive if t.is_owner]
        if owned:
            problems.append(
                f"当前角色 {self.role} 是这些表的 owner，owner 默认绕过自己表上的策略："
                f"{owned}。{_REMEDY_APP_USER}"
            )

        no_rls = [t.table for t in self.tables if not t.rls_enabled]
        if no_rls:
            problems.append(f"这些表没有启用 ROW LEVEL SECURITY：{no_rls}。{_REMEDY_MIGRATE}")

        # RLS 开了但没策略 = 默认拒绝，非 owner 会读到 0 行（表现为"数据丢了"）
        no_policy = [t.table for t in self.tables if t.rls_enabled and not t.has_policy]
        if no_policy:
            problems.append(
                f"这些表启用了 RLS 但没有任何策略，非 owner 角色会读到 0 行："
                f"{no_policy}。{_REMEDY_MIGRATE}"
            )

        # 兜底：上面几条都不成立却仍未生效，说明有本函数没预料到的原因
        unexplained = [
            t.table
            for t in self.inactive
            if not t.is_owner and t.rls_enabled and t.has_policy
        ]
        if unexplained and not (self.is_superuser or self.bypasses_rls):
            problems.append(
                f"这些表策略齐备但 row_security_active() 仍为 false，原因未知：{unexplained}"
            )
        return problems


async def check_rls(session: AsyncSession) -> RlsReport | None:
    """查询 RLS 实际状态。非 Postgres 方言返回 None（SQLite 没有 RLS）。"""
    dialect = session.bind.dialect.name if session.bind else "unknown"
    if dialect != "postgresql":
        return None

    role_row = (await session.execute(_ROLE_QUERY)).one()
    rows = (await session.execute(_TABLE_QUERY)).all()
    return RlsReport(
        role=role_row.role,
        is_superuser=bool(role_row.rolsuper),
        bypasses_rls=bool(role_row.rolbypassrls),
        tables=tuple(
            TableStatus(
                table=r.table_name,
                rls_enabled=bool(r.rls_enabled),
                rls_active=bool(r.rls_active),
                is_owner=bool(r.is_owner),
                has_policy=bool(r.has_policy),
            )
            for r in rows
        ),
    )


async def verify_rls(pg: PostgresStore) -> RlsReport | None:
    """启动自检入口：查一次并把结论写进日志。

    刻意不抛异常 —— 自检本身失败（网络、权限）不该挡住启动，
    否则一个诊断功能反而成了新的单点故障。
    """
    try:
        async with pg.session() as session:
            report = await check_rls(session)
    except Exception as exc:  # 自检失败本身不是致命错误，但要留痕
        logger.error("RLS 自检无法执行", extra={"error": str(exc)})
        return None

    if report is None:
        logger.debug("跳过 RLS 自检：当前方言没有 RLS")
        return None

    if report.ok:
        logger.info(
            "RLS 自检通过",
            extra={"role": report.role, "tables": len(report.tables)},
        )
        return report

    for problem in report.problems():
        logger.error("RLS 未生效", extra={"problem": problem, "role": report.role})
    return report


__all__ = [
    "RlsReport",
    "TableStatus",
    "check_rls",
    "verify_rls",
]
