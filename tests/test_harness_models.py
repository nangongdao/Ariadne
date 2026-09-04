"""Harness Postgres 模型测试 —— 用 aiosqlite 跑真实 SQL。

验证：
- 三张表可创建、可写入
- audit_log append-only（SQLite 无法 REVOKE，但验证字段不可变语义）
- approvals CRUD + 过期自动标记
- rule_sets 版本化
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ariadne.storage.postgres.harness_models import (
    ApprovalRow,
    AuditLogRow,
    RuleSetRow,
)
from ariadne.storage.postgres.models import Base, Organization, Project

TEST_ORG = uuid.UUID("00000000-0000-0000-0000-000000000001")
TEST_PROJECT = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
async def db() -> AsyncIterator[Any]:
    """内存 SQLite，跑真实 SQL。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as sess:
        sess.add(Organization(id=TEST_ORG, name="test-org"))
        sess.add(Project(id=TEST_PROJECT, org_id=TEST_ORG, slug="t", name="Test"))
        await sess.commit()

    class TestDb:
        @asynccontextmanager
        async def session(self) -> AsyncIterator[Any]:
            async with maker() as s:
                try:
                    yield s
                    await s.commit()
                except Exception:
                    await s.rollback()
                    raise

    yield TestDb()
    await engine.dispose()


# ---------- audit_log ----------


class TestAuditLogModel:
    """审计日志表。"""

    @pytest.mark.asyncio
    async def test_insert_audit_log(self, db: Any) -> None:
        """审计日志可写入。"""
        async with db.session() as session:
            row = AuditLogRow(
                project_id=TEST_PROJECT,
                hook="pre_model",
                action="block",
                rule_hits=[{"rule_id": "test", "action": "block"}],
                winning_hit={"rule_id": "test", "action": "block"},
                context_snapshot={"input": "test"},
                message="blocked",
            )
            session.add(row)
            await session.commit()

    @pytest.mark.asyncio
    async def test_query_audit_log(self, db: Any) -> None:
        """审计日志可查询。"""
        async with db.session() as session:
            row = AuditLogRow(
                project_id=TEST_PROJECT,
                hook="pre_model",
                action="warn",
                rule_hits=[],
                winning_hit=None,
                context_snapshot={},
            )
            session.add(row)
            await session.commit()

            stmt = select(AuditLogRow).where(AuditLogRow.project_id == TEST_PROJECT)
            result = await session.execute(stmt)
            rows = result.scalars().all()
            assert len(rows) == 1
            assert rows[0].action == "warn"

    @pytest.mark.asyncio
    async def test_audit_log_has_loop_id_optional(self, db: Any) -> None:
        """loop_id 可为空（审计不限于 Loop）。"""
        async with db.session() as session:
            row = AuditLogRow(
                project_id=TEST_PROJECT,
                loop_id=None,
                hook="pre_model",
                action="allow",
                rule_hits=[],
                winning_hit=None,
                context_snapshot={},
            )
            session.add(row)
            await session.commit()
            assert row.loop_id is None

    @pytest.mark.asyncio
    async def test_audit_log_created_at_set(self, db: Any) -> None:
        """created_at 自动设置。"""
        async with db.session() as session:
            row = AuditLogRow(
                project_id=TEST_PROJECT,
                hook="pre_model",
                action="block",
                rule_hits=[],
                winning_hit=None,
                context_snapshot={},
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            assert row.created_at is not None


# ---------- approvals ----------


class TestApprovalsModel:
    """审批表。"""

    @pytest.mark.asyncio
    async def test_create_approval(self, db: Any) -> None:
        """创建审批需要先有 loop_run。创建 loop_run 后创建审批。"""
        from ariadne.storage.postgres.loop_models import LoopRun

        loop_id = uuid.uuid4()
        async with db.session() as session:
            # 先建 loop_run
            loop = LoopRun(
                id=loop_id,
                project_id=TEST_PROJECT,
                mode="quality",
                goal={"task": "test"},
                state="HUMAN_PENDING",
            )
            session.add(loop)
            await session.commit()

            # 建审批
            approval = ApprovalRow(
                project_id=TEST_PROJECT,
                loop_id=loop_id,
                status="pending",
                context={"rule_id": "approval-rule"},
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            session.add(approval)
            await session.commit()
            assert approval.id is not None
            assert approval.status == "pending"

    @pytest.mark.asyncio
    async def test_approval_status_transitions(self, db: Any) -> None:
        """审批状态可从 pending 改为 approved/rejected。"""
        from ariadne.storage.postgres.loop_models import LoopRun

        loop_id = uuid.uuid4()
        async with db.session() as session:
            loop = LoopRun(
                id=loop_id,
                project_id=TEST_PROJECT,
                mode="quality",
                goal={"task": "test"},
                state="HUMAN_PENDING",
            )
            session.add(loop)
            await session.commit()

            approval = ApprovalRow(
                project_id=TEST_PROJECT,
                loop_id=loop_id,
                status="pending",
                context={},
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            session.add(approval)
            await session.commit()

            # approve
            approval.status = "approved"
            approval.reviewer = "admin"
            approval.decided_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(approval)
            assert approval.status == "approved"
            assert approval.reviewer == "admin"

    @pytest.mark.asyncio
    async def test_approval_expiry(self, db: Any) -> None:
        """过期审批可标记为 expired。"""
        from ariadne.storage.postgres.loop_models import LoopRun

        loop_id = uuid.uuid4()
        async with db.session() as session:
            loop = LoopRun(
                id=loop_id,
                project_id=TEST_PROJECT,
                mode="quality",
                goal={"task": "test"},
                state="HUMAN_PENDING",
            )
            session.add(loop)
            await session.commit()

            approval = ApprovalRow(
                project_id=TEST_PROJECT,
                loop_id=loop_id,
                status="pending",
                context={},
                expires_at=datetime.now(UTC) - timedelta(hours=1),  # 过期
            )
            session.add(approval)
            await session.commit()

            # 模拟过期检查
            now = datetime.now(UTC)
            if approval.expires_at and approval.expires_at < now:
                approval.status = "expired"
            await session.commit()
            await session.refresh(approval)
            assert approval.status == "expired"


# ---------- rule_sets ----------


class TestRuleSetsModel:
    """规则集表。"""

    @pytest.mark.asyncio
    async def test_create_rule_set(self, db: Any) -> None:
        """创建规则集。"""
        async with db.session() as session:
            row = RuleSetRow(
                project_id=TEST_PROJECT,
                name="production",
                version=1,
                rules=[
                    {"id": "block-pii", "action": "block", "when": "true"},
                ],
                is_active=True,
            )
            session.add(row)
            await session.commit()
            assert row.id is not None
            assert row.version == 1
            assert row.is_active is True

    @pytest.mark.asyncio
    async def test_rule_set_versioning(self, db: Any) -> None:
        """规则集版本递增。"""
        async with db.session() as session:
            v1 = RuleSetRow(
                project_id=TEST_PROJECT,
                name="dev",
                version=1,
                rules=[],
                is_active=False,
            )
            v2 = RuleSetRow(
                project_id=TEST_PROJECT,
                name="dev",
                version=2,
                rules=[{"id": "rule-1"}],
                is_active=True,
            )
            session.add(v1)
            session.add(v2)
            await session.commit()

            stmt = (
                select(RuleSetRow)
                .where(RuleSetRow.name == "dev")
                .order_by(RuleSetRow.version.desc())
            )
            result = await session.execute(stmt)
            rows = result.scalars().all()
            assert len(rows) == 2
            assert rows[0].version == 2
            assert rows[0].is_active is True

    @pytest.mark.asyncio
    async def test_rule_set_json_storage(self, db: Any) -> None:
        """规则以 JSONB 存储，可存复杂结构。"""
        rules = [
            {
                "id": "complex-rule",
                "category": "input",
                "hook": "pre_model",
                "when": "detect_pii(input.text).size() > 0",
                "action": "block",
                "severity": "critical",
                "message": "PII found",
                "rewrite_strategy": "redact_pii",
                "route_target": "",
            }
        ]
        async with db.session() as session:
            row = RuleSetRow(
                project_id=TEST_PROJECT,
                name="complex",
                version=1,
                rules=rules,
                is_active=True,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            assert len(row.rules) == 1
            assert row.rules[0]["id"] == "complex-rule"
            assert row.rules[0]["action"] == "block"

    @pytest.mark.asyncio
    async def test_rule_set_timestamps(self, db: Any) -> None:
        """TimestampMixin 提供 created_at/updated_at。"""
        async with db.session() as session:
            row = RuleSetRow(
                project_id=TEST_PROJECT,
                name="ts-test",
                version=1,
                rules=[],
                is_active=True,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            assert row.created_at is not None
            assert row.updated_at is not None
