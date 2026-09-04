"""ClickHouse 迁移版本化测试 —— schema_migrations 过滤逻辑。

与 test_ddl_statements.py 同一思路：不需要真实 ClickHouse 服务端。
`ClickHouseStore.migrate()` 的真实执行路径（bootstrap 连接）在
test_integration.py 覆盖；这里把纯逻辑部件抠出来单独断言：

- `_file_digest`：注释修改不改变 hash（迁移记录不该因改注释而重跑）
- `_ensure_migrations_table`：自举建库 + 建表语句
- `_applied_files`：断言 SELECT 查询文本含 FINAL（ReplacingMergeTree 去重）
- `migrate()` 的过滤决策：stub 连接器后断言「跳过已应用 / 执行未应用 /
  hash 变更重跑 / 全成功才记录 / 二次运行 no-op」
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from ariadne.config import ClickHouseSettings
from ariadne.storage.clickhouse import ClickHouseStore


class FakeClient:
    """记录调用，不真连 ClickHouse。"""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.command_kwargs: list[dict[str, object]] = []

    def command(self, sql: str, **kwargs: object) -> None:
        self.statements.append(sql)
        self.command_kwargs.append(kwargs)

    def query(self, sql: str, **kwargs: object) -> object:
        self.statements.append(sql)
        return FakeResult()

    def close(self) -> None:
        pass


class FakeResult:
    result_rows: ClassVar[list[tuple[object, object]]] = []


class TestFileDigest:
    def test_comment_change_keeps_digest(self) -> None:
        """只改注释不该触发文件重跑 —— 注释不是 schema 变更。"""
        a = "-- 旧注释\nCREATE TABLE t (a UInt8) ENGINE = Memory"
        b = "-- 新注释（只是措辞）\nCREATE TABLE t (a UInt8) ENGINE = Memory"
        assert ClickHouseStore._file_digest(a) == ClickHouseStore._file_digest(b)

    def test_statement_change_changes_digest(self) -> None:
        a = "CREATE TABLE t (a UInt8) ENGINE = Memory"
        b = "CREATE TABLE t (a UInt64) ENGINE = Memory"
        assert ClickHouseStore._file_digest(a) != ClickHouseStore._file_digest(b)

    def test_digest_is_sha256_hex(self) -> None:
        digest = ClickHouseStore._file_digest("SELECT 1")
        assert len(digest) == 64
        int(digest, 16)  # 全 hex，不抛错


class TestMigrationsTable:
    def test_bootstrap_database_before_table(self) -> None:
        """建表前必须先确保库存在 —— 全新部署时 ariadne 库还不存在。

        001_spans.sql 里的 CREATE DATABASE 要到循环里才轮得到，不能拿它
        当迁移表自身的先决条件。
        """
        client = FakeClient()
        ClickHouseStore._ensure_migrations_table(client)
        assert client.statements[0] == "CREATE DATABASE IF NOT EXISTS ariadne"
        assert "CREATE TABLE IF NOT EXISTS ariadne.schema_migrations" in (
            client.statements[1]
        )

    def test_merge_engine_dedupes_by_file_name(self) -> None:
        client = FakeClient()
        ClickHouseStore._ensure_migrations_table(client)
        sql = client.statements[1]
        assert "ReplacingMergeTree" in sql
        assert "ORDER BY file_name" in sql


class TestAppliedFilesQuery:
    def test_query_uses_final_for_dedup(self) -> None:
        """ReplacingMergeTree 不 FINAL 会把重复行当「已应用」结果返回。"""
        client = FakeClient()
        ClickHouseStore._applied_files(client)
        sql = client.statements[0]
        assert "schema_migrations FINAL" in sql


class TestMigrateFiltering:
    """migrate() 的过滤决策 —— stub 掉连接与查询层，只暴露过滤逻辑。"""

    @staticmethod
    def _stub(
        monkeypatch: pytest.MonkeyPatch,
        *,
        recorded: dict[str, str],
        files: dict[str, str],
    ) -> tuple[Path, FakeClient]:
        import tempfile

        tmp = Path(tempfile.mkdtemp())
        for name, content in files.items():
            (tmp / name).write_text(content, encoding="utf-8")

        client = FakeClient()
        monkeypatch.setattr(
            "ariadne.storage.clickhouse.clickhouse_connect.get_client",
            lambda **kwargs: client,
        )
        monkeypatch.setattr(
            "ariadne.storage.clickhouse.ClickHouseStore._applied_files",
            lambda self, c: dict(recorded),
        )
        monkeypatch.setattr(
            "ariadne.storage.clickhouse.ClickHouseStore._ensure_migrations_table",
            lambda self, c: None,
        )
        return tmp, client

    def test_skips_files_with_matching_hash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        files = {
            "001_a.sql": "CREATE TABLE t1 (a UInt8) ENGINE = Memory",
            "002_b.sql": "CREATE TABLE t2 (b UInt8) ENGINE = Memory",
        }
        tmp, client = self._stub(
            monkeypatch,
            recorded={
                "001_a.sql": ClickHouseStore._file_digest(files["001_a.sql"]),
            },
            files=files,
        )

        applied = ClickHouseStore(ClickHouseSettings()).migrate(tmp)

        assert applied == ["002_b.sql"]
        # 002 执行 + 记录；001 完全没碰（无 DDL、无 INSERT）
        assert client.statements == [
            "CREATE TABLE t2 (b UInt8) ENGINE = Memory",
            "INSERT INTO ariadne.schema_migrations"
            " (file_name, applied_at, content_hash) VALUES"
            " ({name:String}, {at:DateTime64(3, 'UTC')}, {hash:String})",
        ]

    def test_hash_change_reruns_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """记录里的 hash 与当前文件不一致 → 视为未应用，整体重跑。"""
        files = {"001_a.sql": "CREATE TABLE t1 (a UInt8) ENGINE = Memory"}
        tmp, client = self._stub(
            monkeypatch,
            recorded={"001_a.sql": "0" * 64},  # 过期 hash
            files=files,
        )

        applied = ClickHouseStore(ClickHouseSettings()).migrate(tmp)

        assert applied == ["001_a.sql"]
        assert "CREATE TABLE t1 (a UInt8) ENGINE = Memory" in client.statements

    def test_empty_record_is_first_run_applies_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """历史库没有迁移表 → 空记录 = 全量应用（升级路径平滑）。"""
        files = {
            "001_a.sql": "CREATE TABLE t1 (a UInt8) ENGINE = Memory",
            "002_b.sql": "CREATE TABLE t2 (b UInt8) ENGINE = Memory",
        }
        tmp, client = self._stub(monkeypatch, recorded={}, files=files)

        applied = ClickHouseStore(ClickHouseSettings()).migrate(tmp)

        assert applied == ["001_a.sql", "002_b.sql"]
        # 每个文件各一条 DDL + 一条 INSERT 记录
        assert len(client.statements) == 4

    def test_records_after_success_with_hash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        files = {"001_a.sql": "CREATE TABLE t1 (a UInt8) ENGINE = Memory"}
        tmp, client = self._stub(monkeypatch, recorded={}, files=files)

        ClickHouseStore(ClickHouseSettings()).migrate(tmp)

        insert = [
            (sql, kw)
            for sql, kw in zip(client.statements, client.command_kwargs, strict=True)
            if sql.startswith("INSERT INTO ariadne.schema_migrations")
        ]
        assert len(insert) == 1
        sql, kw = insert[0]
        assert "{name:String}" in sql and "{hash:String}" in sql
        params: dict[str, object] = kw["parameters"]  # type: ignore[assignment]
        assert params["name"] == "001_a.sql"
        assert params["hash"] == ClickHouseStore._file_digest(files["001_a.sql"])

    def test_all_statements_in_file_executed_before_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """文件内语句全部执行成功才 INSERT —— 中途失败下次重跑整个文件。"""
        files = {"001_a.sql": "SELECT 1; SELECT 2"}
        tmp, client = self._stub(monkeypatch, recorded={}, files=files)

        ClickHouseStore(ClickHouseSettings()).migrate(tmp)

        ddl = [s for s in client.statements if not s.startswith("INSERT")]
        assert ddl == ["SELECT 1", "SELECT 2"]
        # INSERT 必须是最后一个调用（语句全成功之后）
        assert client.statements[-1].startswith("INSERT INTO ariadne.schema_migrations")

    def test_second_run_skips_everything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """连续两次运行的完整闭环：第一次执行 + 记录，第二次全部跳过。

        这正是 compose 每次启动都跑 ariadne-migrate 的行为：首启全量，
        之后每次都是 no-op。
        """
        files = {"001_a.sql": "CREATE TABLE t1 (a UInt8) ENGINE = Memory"}
        tmp, client = self._stub(monkeypatch, recorded={}, files=files)

        ClickHouseStore(ClickHouseSettings()).migrate(tmp)
        insert = [
            kw["parameters"]
            for sql, kw in zip(client.statements, client.command_kwargs, strict=True)
            if sql.startswith("INSERT INTO ariadne.schema_migrations")
        ]
        assert len(insert) == 1
        params: dict[str, object] = insert[0]  # type: ignore[assignment]

        # 第二次运行：记录来自第一次的 INSERT（模拟查询 schema_migrations）
        tmp2, client2 = self._stub(
            monkeypatch,
            recorded={str(params["name"]): str(params["hash"])},
            files=files,
        )
        applied = ClickHouseStore(ClickHouseSettings()).migrate(tmp2)

        assert applied == []
        assert client2.statements == []  # 没有任何 DDL 或 INSERT