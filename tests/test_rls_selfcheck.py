"""RLS 自检的诊断逻辑测试。

problems() 是纯函数，所以三条静默失效路径（owner 回落 / 迁移未跑完 /
角色带 BYPASSRLS）都能在没有真实 PG 的情况下逐条验证。

不覆盖的部分（需要真实 PG，本环境没有 Docker）：_TABLE_QUERY 与
_ROLE_QUERY 的 SQL 正确性、row_security_active() 的实际返回值。
这两条要靠 -m integration 的用例兜，见 tests/test_auth_integration.py
的 TestSelfCheckAgainstRealPostgres。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest import mock

import pytest

from ariadne.auth.rls import RlsReport, TableStatus, check_rls, verify_rls


def _table(
    name: str = "datasets",
    *,
    rls_enabled: bool = True,
    rls_active: bool = True,
    is_owner: bool = False,
    has_policy: bool = True,
) -> TableStatus:
    """默认构造一张"RLS 正常生效"的表，各测试只改它关心的那一维。"""
    return TableStatus(
        table=name,
        rls_enabled=rls_enabled,
        rls_active=rls_active,
        is_owner=is_owner,
        has_policy=has_policy,
    )


# 角色名用哨兵：remedy 文本里含 "ariadne_app"，拿真实角色名断言时
# "ariadne" 会被 remedy 的子串满足，消息里删掉角色名也照样通过（变异检查抓到过）。
_APP_ROLE = "approle-sentinel"
_OWNER_ROLE = "ownerrole-sentinel"
_SUPER_ROLE = "superrole-sentinel"


def _report(
    *tables: TableStatus,
    role: str = _APP_ROLE,
    is_superuser: bool = False,
    bypasses_rls: bool = False,
) -> RlsReport:
    return RlsReport(
        role=role,
        is_superuser=is_superuser,
        bypasses_rls=bypasses_rls,
        tables=tables,
    )


class TestHealthyReport:
    """先证明"正常"确实判为正常，否则下面的失败断言分不清真假。"""

    def test_non_owner_with_policies_is_ok(self) -> None:
        report = _report(_table("datasets"), _table("experiments"))
        assert report.problems() == []
        assert report.ok

    def test_inactive_is_empty_when_healthy(self) -> None:
        assert _report(_table()).inactive == ()


class TestEmptyTableListIsAFailure:
    """空清单必须报错 —— all() 对空集恒真，这是自检最容易空过的形态。"""

    def test_no_tables_is_not_ok(self) -> None:
        report = _report()
        assert not report.ok
        assert len(report.problems()) == 1
        assert "迁移没跑" in report.problems()[0]

    def test_empty_report_mentions_migrate_remedy(self) -> None:
        assert "ariadne-migrate" in _report().problems()[0]


class TestOwnerFallback:
    """最危险的一条：app_user 留空回落 owner，连接成功且 RLS 静默失效。"""

    def test_owner_tables_are_reported(self) -> None:
        report = _report(
            _table("datasets", rls_active=False, is_owner=True),
            _table("experiments", rls_active=False, is_owner=True),
            role=_OWNER_ROLE,
        )
        assert not report.ok
        problem = next(p for p in report.problems() if "owner" in p)
        assert "datasets" in problem
        assert "experiments" in problem
        assert _OWNER_ROLE in problem

    def test_owner_problem_points_at_app_user_env(self) -> None:
        """诊断必须给出可执行的修法，而不只是"RLS 没生效"。"""
        report = _report(_table(rls_active=False, is_owner=True), role=_OWNER_ROLE)
        problem = next(p for p in report.problems() if "owner" in p)
        assert "ARIADNE_PG_APP_USER" in problem
        assert "ariadne_app" in problem

    def test_owner_of_table_where_rls_still_active_is_not_flagged(self) -> None:
        """FORCE ROW LEVEL SECURITY 下 owner 也受策略约束，此时不该报。"""
        report = _report(_table(is_owner=True, rls_active=True))
        assert report.ok


class TestRoleAttributes:
    def test_superuser_is_flagged(self) -> None:
        report = _report(_table(rls_active=False), role=_SUPER_ROLE, is_superuser=True)
        problem = next(p for p in report.problems() if "superuser" in p)
        assert _SUPER_ROLE in problem

    def test_bypassrls_is_flagged(self) -> None:
        report = _report(_table(rls_active=False), bypasses_rls=True)
        problem = next(p for p in report.problems() if "BYPASSRLS" in p)
        assert _APP_ROLE in problem

    def test_role_attribute_suppresses_unexplained_branch(self) -> None:
        """角色属性已经解释了失效原因，不该再报"原因未知"。"""
        report = _report(_table(rls_active=False), bypasses_rls=True)
        assert not any("原因未知" in p for p in report.problems())


class TestMigrationNotApplied:
    def test_rls_disabled_is_flagged(self) -> None:
        report = _report(
            _table("datasets"),
            _table("loop_checkpoints", rls_enabled=False, rls_active=False, has_policy=False),
        )
        problem = next(p for p in report.problems() if "没有启用" in p)
        assert "loop_checkpoints" in problem
        assert "datasets" not in problem

    def test_enabled_without_policy_is_flagged_as_blackout(self) -> None:
        """RLS 开了没策略 = 默认拒绝，表现是"数据丢了"而非权限报错。"""
        report = _report(_table("audit_log", has_policy=False, rls_active=False))
        problem = next(p for p in report.problems() if "0 行" in p)
        assert "audit_log" in problem

    def test_disabled_table_not_double_reported_as_missing_policy(self) -> None:
        """没开 RLS 的表只报一次，不该同时算进"缺策略"。"""
        report = _report(_table(rls_enabled=False, rls_active=False, has_policy=False))
        assert not any("0 行" in p for p in report.problems())


class TestUnexplainedFallback:
    def test_active_false_with_everything_in_place_is_reported(self) -> None:
        """策略齐备、非 owner、角色干净却仍不生效 —— 兜底分支必须发声。"""
        report = _report(_table("datasets", rls_active=False))
        assert not report.ok
        assert any("原因未知" in p for p in report.problems())


class TestCheckRlsOnSqlite:
    """None 有三条来路（方言短路 / 空报告 / 异常被吞），测试必须能分辨是哪条。"""

    async def test_returns_none_for_sqlite(self, memory_pg: Any) -> None:
        """SQLite 没有 RLS，自检必须短路而不是抛 SQL 语法错。"""
        async with memory_pg.session() as session:
            # 先证明 bind 真的挂着引擎：若 session.bind 为 None，dialect 会是
            # "unknown"，也照样返回 None —— 那样这条断言就成了空过。
            assert session.bind is not None
            assert session.bind.dialect.name == "sqlite"
            assert await check_rls(session) is None

    async def test_verify_rls_takes_the_dialect_shortcut_not_the_except_branch(
        self, memory_pg: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """走的必须是方言短路，而不是"抛异常被 except 吞掉"。"""
        with caplog.at_level("DEBUG", logger="ariadne.auth.rls"):
            assert await verify_rls(memory_pg) is None
        messages = [r.message for r in caplog.records]
        assert "跳过 RLS 自检：当前方言没有 RLS" in messages
        assert not [r for r in caplog.records if r.levelname == "ERROR"]


class TestVerifyRlsSwallowsErrors:
    async def test_connection_failure_does_not_raise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """自检本身失败不该挡住启动 —— 诊断功能不能变成新的单点故障。"""

        class BrokenStore:
            def session(self) -> object:
                raise OSError("connection refused")

        with caplog.at_level("ERROR", logger="ariadne.auth.rls"):
            assert await verify_rls(BrokenStore()) is None  # type: ignore[arg-type]
        # 断言留痕：静默吞掉异常和"吞掉但记日志"是两种完全不同的行为
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 1
        assert "connection refused" in errors[0].error

    async def test_problems_are_logged_one_line_each(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """失效时每条根因单独一行 —— 拼成一坨会让日志检索抓不到具体表名。"""

        class OwnerStore:
            @staticmethod
            def _report() -> RlsReport:
                return _report(
                    _table("datasets", rls_active=False, is_owner=True),
                    role=_OWNER_ROLE,
                    is_superuser=True,
                )

            @asynccontextmanager
            async def session(self) -> AsyncIterator[Any]:
                yield object()

        report = OwnerStore._report()
        assert len(report.problems()) == 2  # superuser + owner，下面要求两条日志

        with (
            mock.patch("ariadne.auth.rls.check_rls", return_value=report),
            caplog.at_level("ERROR", logger="ariadne.auth.rls"),
        ):
            returned = await verify_rls(OwnerStore())  # type: ignore[arg-type]

        assert returned is report
        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(errors) == 2
        assert all(r.role == _OWNER_ROLE for r in errors)
        assert any("datasets" in r.problem for r in errors)
