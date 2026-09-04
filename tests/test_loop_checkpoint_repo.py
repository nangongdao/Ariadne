"""Loop 检查点仓储测试。

用 aiosqlite 跑真实 SQL：建表、外键、JSON 序列化往返、幂等写。
Postgres 特有行为（ON CONFLICT、部分索引）留给 -m integration。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ariadne.loop_module.budget import BudgetUsage
from ariadne.loop_module.checkpoint import Checkpoint
from ariadne.loop_module.critique import Critique
from ariadne.loop_module.fingerprint import IterationTrace
from ariadne.loop_module.goal import AssertionKind
from ariadne.loop_module.state_machine import LoopState
from ariadne.storage.postgres.loop_models import LoopRun
from ariadne.storage.postgres.models import Base, Organization, Project
from ariadne.storage.postgres.repositories.loop_checkpoint_repo import (
    LoopCheckpointRepository,
)

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
LOOP_ID = "00000000-0000-0000-0000-000000000010"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as sess:
        sess.add(Organization(id=ORG_ID, name="test-org"))
        sess.add(Project(id=PROJECT_ID, org_id=ORG_ID, slug="test", name="Test"))
        # LoopRun 外键父行
        sess.add(
            LoopRun(
                id=uuid.UUID(LOOP_ID),
                project_id=PROJECT_ID,
                mode="quality",
                goal={"task": "test"},
                state="CREATED",
            )
        )
        await sess.commit()
        yield sess

    await engine.dispose()


def make_checkpoint(iteration: int) -> Checkpoint:
    """构造一个含完整字段的检查点，验证序列化往返不丢信息。"""
    from ariadne.loop_module.verifier.base import AssertionOutcome, Verdict

    verdict = Verdict(
        converged=False,
        passed=("a1",),
        failed=(
            AssertionOutcome(
                assertion_id="a2",
                kind=AssertionKind.REGEX,
                passed=False,
                value=0.0,
                evidence="未匹配",
            ),
        ),
        score=55.0,
        claimed_done=True,
        outcomes=(
            AssertionOutcome(assertion_id="a1", kind=AssertionKind.REGEX, passed=True),
            AssertionOutcome(assertion_id="a2", kind=AssertionKind.REGEX, passed=False),
        ),
    )
    critique = Critique(
        failures=("格式断言失败",),
        directives=("添加函数定义",),
        forbidden=("轮次1：试过加注释",),
    )
    return Checkpoint(
        loop_id=LOOP_ID,
        iteration=iteration,
        state=LoopState.JUDGING,
        usage=BudgetUsage(total_tokens=1234, cost_micro_usd=56_000),
        output_fp="abc123",
        failure_fp="def456",
        verdict=verdict,
        critique=critique,
        last_output="def f(): pass",
        previous_output="no func",
        history=(
            IterationTrace(iteration=1, output_fp="aaa", failure_fp="fff", score=50.0),
            IterationTrace(iteration=2, output_fp="bbb", failure_fp="fff", score=55.0),
        ),
        critique_history=(critique,),
    )


class TestCheckpointRoundtrip:
    async def test_save_and_latest(self, session: AsyncSession) -> None:
        """存检查点再取最新，字段完整保留。"""
        repo = LoopCheckpointRepository(session)
        cp = make_checkpoint(1)
        await repo.save(cp, project_id=PROJECT_ID)

        loaded = await repo.latest(LOOP_ID, project_id=PROJECT_ID)
        assert loaded is not None
        assert loaded.iteration == 1
        assert loaded.state is LoopState.JUDGING
        assert loaded.output_fp == "abc123"
        assert loaded.failure_fp == "def456"
        assert loaded.usage.total_tokens == 1234
        # cost_micro_usd 从 float(Numeric) 往返
        assert loaded.usage.cost_usd == Decimal("0.056")

    async def test_verdict_roundtrip(self, session: AsyncSession) -> None:
        """Verdict 含 outcomes/failed，序列化往返不能丢。"""
        repo = LoopCheckpointRepository(session)
        cp = make_checkpoint(1)
        await repo.save(cp, project_id=PROJECT_ID)

        loaded = await repo.latest(LOOP_ID, project_id=PROJECT_ID)
        assert loaded is not None
        assert loaded.verdict is not None
        assert loaded.verdict.converged is False
        assert loaded.verdict.score == 55.0
        assert loaded.verdict.claimed_done is True
        assert loaded.verdict.false_completion is True
        assert len(loaded.verdict.outcomes) == 2
        # 枚举还原
        assert loaded.verdict.outcomes[1].kind is AssertionKind.REGEX
        assert loaded.verdict.failed[0].evidence == "未匹配"

    async def test_critique_and_history_roundtrip(self, session: AsyncSession) -> None:
        """Critique 与振荡历史往返不丢（崩溃恢复依赖）。"""
        repo = LoopCheckpointRepository(session)
        cp = make_checkpoint(1)
        await repo.save(cp, project_id=PROJECT_ID)

        loaded = await repo.latest(LOOP_ID, project_id=PROJECT_ID)
        assert loaded is not None
        assert loaded.critique is not None
        assert "格式断言失败" in loaded.critique.failures
        assert len(loaded.history) == 2
        assert loaded.history[1].score == 55.0
        assert len(loaded.critique_history) == 1

    async def test_output_text_preserved(self, session: AsyncSession) -> None:
        """last_output/previous_output 塞在 verdict JSON 里，往返要取回。"""
        repo = LoopCheckpointRepository(session)
        cp = make_checkpoint(1)
        await repo.save(cp, project_id=PROJECT_ID)

        loaded = await repo.latest(LOOP_ID, project_id=PROJECT_ID)
        assert loaded is not None
        assert loaded.last_output == "def f(): pass"
        assert loaded.previous_output == "no func"


class TestIdempotentWrite:
    async def test_rewrite_same_iteration_overwrites(self, session: AsyncSession) -> None:
        """验收项：save 幂等 —— 重写同一 (loop_id, iteration) 不产生两条。"""
        repo = LoopCheckpointRepository(session)
        await repo.save(make_checkpoint(1), project_id=PROJECT_ID)
        # 改一些字段重写
        cp2 = make_checkpoint(1)
        # 替换为不可变检查点的新实例（改 output_fp）
        from dataclasses import replace

        cp2_revised = replace(cp2, output_fp="new_fp")
        await repo.save(cp2_revised, project_id=PROJECT_ID)

        loaded = await repo.latest(LOOP_ID, project_id=PROJECT_ID)
        assert loaded is not None
        assert loaded.output_fp == "new_fp"  # 被覆盖，不是旧行
        # latest 仍是 iteration=1，没多出 iteration=1 的重复行
        assert loaded.iteration == 1

    async def test_latest_takes_max_iteration(self, session: AsyncSession) -> None:
        """latest 取最大 iteration —— 崩溃恢复读最新检查点。"""
        repo = LoopCheckpointRepository(session)
        await repo.save(make_checkpoint(1), project_id=PROJECT_ID)
        await repo.save(make_checkpoint(2), project_id=PROJECT_ID)
        await repo.save(make_checkpoint(3), project_id=PROJECT_ID)

        loaded = await repo.latest(LOOP_ID, project_id=PROJECT_ID)
        assert loaded is not None
        assert loaded.iteration == 3

    async def test_missing_loop_returns_none(self, session: AsyncSession) -> None:
        """无检查点的 Loop 返回 None —— engine 视为首启动。"""
        repo = LoopCheckpointRepository(session)
        loaded = await repo.latest(
            "00000000-0000-0000-0000-000000000099", project_id=PROJECT_ID
        )
        assert loaded is None


class TestCheckpointStoreContract:
    async def test_implements_protocol(self, session: AsyncSession) -> None:
        """LoopCheckpointRepository 满足 CheckpointStore Protocol 契约。"""
        from ariadne.loop_module.checkpoint import CheckpointStore

        repo: CheckpointStore = LoopCheckpointRepository(session)
        # save/latest 方法存在且可调用（Protocol 结构性校验）
        assert hasattr(repo, "save")
        assert hasattr(repo, "latest")
