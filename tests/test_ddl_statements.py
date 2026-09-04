"""iter_ddl_statements 的回归护栏。

存在理由：这个函数曾经把注释判断写成「整段是否以 -- 开头」。按分号切分后每条
语句都连着它上方的注释行，于是凡是带注释的语句都被整条丢弃 —— 建库、建表、TTL
全没执行，只剩下不带注释的那几条，而 migrate() 依然把文件记为「已应用」，没有
任何报错。交付的 16 条语句里大部分都带注释，所以受影响面是大头而非个例。

这批断言不需要 ClickHouse 服务端：函数是纯字符串处理，故意提到模块级就是为了
能在这里单独测。
"""

from __future__ import annotations

from pathlib import Path

from ariadne.storage.clickhouse import iter_ddl_statements

DDL_DIR = Path(__file__).resolve().parents[1] / "deploy" / "clickhouse"


class TestCommentStripping:
    def test_statement_preceded_by_comment_survives(self) -> None:
        """回归锚点：注释在语句上方时，语句本身不能被丢掉。"""
        sql = "-- 建库\nCREATE DATABASE ariadne;"
        assert list(iter_ddl_statements(sql)) == ["CREATE DATABASE ariadne"]

    def test_multiple_comment_lines_above_statement(self) -> None:
        sql = "-- 一\n-- 二\n-- 三\nCREATE TABLE t (a UInt8) ENGINE = Memory;"
        assert list(iter_ddl_statements(sql)) == [
            "CREATE TABLE t (a UInt8) ENGINE = Memory"
        ]

    def test_comment_only_chunk_is_dropped(self) -> None:
        """纯注释段不产出，否则会把注释当 SQL 发给服务端。"""
        assert list(iter_ddl_statements("-- 只有注释，没有语句\n")) == []

    def test_indented_comment_is_stripped(self) -> None:
        """行内缩进的注释也算注释（strip 后再判前缀）。"""
        sql = "CREATE TABLE t (\n    a UInt8\n    -- 缩进注释\n) ENGINE = Memory;"
        assert list(iter_ddl_statements(sql)) == [
            "CREATE TABLE t (\n    a UInt8\n) ENGINE = Memory"
        ]

    def test_trailing_comment_after_code_is_kept(self) -> None:
        """行尾注释不剥：只按行首判断，剥掉会破坏同行的 SQL。"""
        stmts = list(iter_ddl_statements("SELECT 1;  -- 行尾说明\n"))
        assert stmts == ["SELECT 1"]


class TestSplitting:
    def test_multiple_statements(self) -> None:
        sql = "CREATE DATABASE a;\n-- 注释\nCREATE TABLE a.t (x UInt8) ENGINE = Memory;"
        assert list(iter_ddl_statements(sql)) == [
            "CREATE DATABASE a",
            "CREATE TABLE a.t (x UInt8) ENGINE = Memory",
        ]

    def test_empty_input(self) -> None:
        assert list(iter_ddl_statements("")) == []

    def test_whitespace_only(self) -> None:
        assert list(iter_ddl_statements("\n\n   \n")) == []

    def test_trailing_semicolon_yields_no_empty_statement(self) -> None:
        assert list(iter_ddl_statements("SELECT 1;\n")) == ["SELECT 1"]

    def test_no_trailing_semicolon(self) -> None:
        """最后一条不带分号也要产出。"""
        assert list(iter_ddl_statements("SELECT 1")) == ["SELECT 1"]

    def test_multiline_statement_preserved(self) -> None:
        sql = "ALTER TABLE t\n    MODIFY TTL\n        d + INTERVAL 1 DAY DELETE;"
        assert list(iter_ddl_statements(sql)) == [
            "ALTER TABLE t\n    MODIFY TTL\n        d + INTERVAL 1 DAY DELETE"
        ]


class TestShippedDdl:
    """对真实交付的 DDL 文件做计数校验，防止再出现「静默少执行」。"""

    def test_every_shipped_file_yields_statements(self) -> None:
        files = sorted(DDL_DIR.glob("*.sql"))
        assert files, f"没找到 DDL 文件：{DDL_DIR}"
        for path in files:
            stmts = list(iter_ddl_statements(path.read_text(encoding="utf-8")))
            assert stmts, f"{path.name} 产出 0 条语句"

    def test_shipped_statement_counts(self) -> None:
        """锚定条数。改 DDL 时这里会失败，提醒确认是有意增删。"""
        counts = {
            path.name: len(list(iter_ddl_statements(path.read_text(encoding="utf-8"))))
            for path in sorted(DDL_DIR.glob("*.sql"))
        }
        assert counts == {
            # 建库 + spans + trace_rollup + mv + cost_rollup + mv
            "001_spans.sql": 6,
            # 三张表各一对 DROP POLICY / CREATE ROW POLICY
            "002_row_policies.sql": 6,
            # storage_policy + spans 两级 TTL + 两张 rollup 的 TTL
            "003_retention.sql": 4,
            # cost_rollup 两列 ALTER + DROP VIEW + CREATE MV（P1-9）
            "004_cost_token_breakdown.sql": 4,
        }

    def test_no_statement_starts_with_comment_marker(self) -> None:
        """产出物里不能残留注释开头 —— 那说明剥离逻辑又漏了。"""
        for path in sorted(DDL_DIR.glob("*.sql")):
            for stmt in iter_ddl_statements(path.read_text(encoding="utf-8")):
                assert not stmt.startswith("--"), f"{path.name}: {stmt[:60]}"
