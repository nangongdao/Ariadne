"""审计 project_id + loop_id 归因测试。

验证：
- AuditRecord 携带 project_id 和 loop_id
- PostgresAuditSink 写入 audit_log 时填充 project_id + loop_id
- AuditLogRow.project_id NOT NULL 约束生效
- 跨租户审计记录隔离
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ariadne.harness_module.audit import AuditRecord, InMemoryAuditSink
from ariadne.harness_module.models import Action, HookKind, Rule, RuleCategory, RuleHit

PROJECT_A = uuid.UUID("00000000-0000-0000-0000-000000000001")
PROJECT_B = uuid.UUID("00000000-0000-0000-0000-000000000002")
LOOP_ID = "00000000-0000-0000-0000-00000000aaaa"


def _make_hit() -> RuleHit:
    return RuleHit(
        rule=Rule(
            id="test-rule",
            category=RuleCategory.INPUT,
            hook=HookKind.PRE_MODEL,
            when="true",
        ),
        value=True,
        message="test",
    )


class TestAuditRecordFields:
    def test_record_has_project_id(self) -> None:
        record = AuditRecord.create(
            project_id=PROJECT_A,
            hook=HookKind.PRE_MODEL,
            action=Action.ALLOW,
            rule_hits=(),
            winning_hit=None,
            context_snapshot={},
        )
        assert record.project_id == PROJECT_A

    def test_record_has_loop_id(self) -> None:
        record = AuditRecord.create(
            project_id=PROJECT_A,
            hook=HookKind.PRE_MODEL,
            action=Action.ALLOW,
            rule_hits=(),
            winning_hit=None,
            context_snapshot={},
            loop_id=str(LOOP_ID),
        )
        assert record.loop_id == str(LOOP_ID)

    def test_record_loop_id_defaults_empty(self) -> None:
        record = AuditRecord.create(
            project_id=PROJECT_A,
            hook=HookKind.PRE_MODEL,
            action=Action.ALLOW,
            rule_hits=(),
            winning_hit=None,
            context_snapshot={},
        )
        assert record.loop_id == ""

    def test_record_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        record = AuditRecord.create(
            project_id=PROJECT_A,
            hook=HookKind.PRE_MODEL,
            action=Action.ALLOW,
            rule_hits=(),
            winning_hit=None,
            context_snapshot={},
        )
        with pytest.raises(FrozenInstanceError):
            record.action = Action.BLOCK  # type: ignore[misc]


class TestInMemoryAuditSink:
    async def test_write_preserves_project_id(self) -> None:
        sink = InMemoryAuditSink()
        record = AuditRecord.create(
            project_id=PROJECT_A,
            hook=HookKind.PRE_MODEL,
            action=Action.ALLOW,
            rule_hits=(_make_hit(),),
            winning_hit=_make_hit(),
            context_snapshot={"iteration": 1},
            loop_id=str(LOOP_ID),
        )
        await sink.write(record)

        assert sink.count == 1
        latest = sink.latest()
        assert latest is not None
        assert latest.project_id == PROJECT_A
        assert latest.loop_id == str(LOOP_ID)

    async def test_multiple_writes_accumulate(self) -> None:
        sink = InMemoryAuditSink()
        for i in range(5):
            record = AuditRecord.create(
                project_id=PROJECT_A,
                hook=HookKind.PRE_MODEL,
                action=Action.ALLOW,
                rule_hits=(),
                winning_hit=None,
                context_snapshot={"iteration": i},
                loop_id=str(LOOP_ID),
            )
            await sink.write(record)

        assert sink.count == 5


class TestPostgresAuditSink:
    async def test_write_fills_project_id_and_loop_id(self, memory_pg: Any) -> None:
        from ariadne.harness_module.audit import PostgresAuditSink

        sink = PostgresAuditSink(memory_pg)
        record = AuditRecord.create(
            project_id=PROJECT_A,
            hook=HookKind.PRE_MODEL,
            action=Action.BLOCK,
            rule_hits=(_make_hit(),),
            winning_hit=_make_hit(),
            context_snapshot={"iteration": 0},
            loop_id=str(LOOP_ID),
        )
        await sink.write(record)

        # 验证写入成功
        from sqlalchemy import select

        from ariadne.storage.postgres.harness_models import AuditLogRow

        async with memory_pg.session() as session:
            result = await session.execute(select(AuditLogRow))
            rows = result.scalars().all()
            assert len(rows) == 1
            row = rows[0]
            assert row.project_id == PROJECT_A
            assert row.loop_id == uuid.UUID(LOOP_ID)
            assert row.hook == "pre_model"
            assert row.action == "block"
            assert row.message == ""

    async def test_write_with_empty_loop_id(self, memory_pg: Any) -> None:
        from ariadne.harness_module.audit import PostgresAuditSink

        sink = PostgresAuditSink(memory_pg)
        record = AuditRecord.create(
            project_id=PROJECT_A,
            hook=HookKind.PRE_MODEL,
            action=Action.ALLOW,
            rule_hits=(),
            winning_hit=None,
            context_snapshot={},
            # loop_id 默认为空字符串
        )
        await sink.write(record)

        from sqlalchemy import select

        from ariadne.storage.postgres.harness_models import AuditLogRow

        async with memory_pg.session() as session:
            result = await session.execute(select(AuditLogRow))
            rows = result.scalars().all()
            assert len(rows) == 1
            assert rows[0].loop_id is None  # 空 loop_id → None

    async def test_write_with_invalid_loop_id(self, memory_pg: Any) -> None:
        """非 UUID 格式的 loop_id 不报错，loop_id 列存 None。"""
        from ariadne.harness_module.audit import PostgresAuditSink

        sink = PostgresAuditSink(memory_pg)
        record = AuditRecord.create(
            project_id=PROJECT_A,
            hook=HookKind.PRE_MODEL,
            action=Action.ALLOW,
            rule_hits=(),
            winning_hit=None,
            context_snapshot={},
            loop_id="not-a-uuid",
        )
        await sink.write(record)

        from sqlalchemy import select

        from ariadne.storage.postgres.harness_models import AuditLogRow

        async with memory_pg.session() as session:
            result = await session.execute(select(AuditLogRow))
            rows = result.scalars().all()
            assert len(rows) == 1
            assert rows[0].loop_id is None

    async def test_cross_tenant_audit_isolation(self, memory_pg: Any) -> None:
        """A 租户的审计记录在 B 租户查询时不可见（应用层过滤）。"""
        from ariadne.harness_module.audit import PostgresAuditSink

        sink = PostgresAuditSink(memory_pg)
        # 写 A 租户审计记录
        record_a = AuditRecord.create(
            project_id=PROJECT_A,
            hook=HookKind.PRE_MODEL,
            action=Action.BLOCK,
            rule_hits=(),
            winning_hit=None,
            context_snapshot={},
            loop_id=str(LOOP_ID),
        )
        await sink.write(record_a)

        # 查 B 租户的审计记录（应用层过滤）
        from sqlalchemy import select

        from ariadne.storage.postgres.harness_models import AuditLogRow

        async with memory_pg.session() as session:
            result = await session.execute(
                select(AuditLogRow).where(AuditLogRow.project_id == PROJECT_B)
            )
            rows_b = result.scalars().all()
            assert len(rows_b) == 0

            result_a = await session.execute(
                select(AuditLogRow).where(AuditLogRow.project_id == PROJECT_A)
            )
            rows_a = result_a.scalars().all()
            assert len(rows_a) == 1
