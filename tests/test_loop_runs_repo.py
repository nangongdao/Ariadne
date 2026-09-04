"""loop_runs 仓储测试。

用共享内存 SQLite 跑真实 SQL：建表、外键、序列化往返、租约与终态。
Postgres 特有行为（部分索引、JSONB）留给 -m integration。

覆盖的核心契约：
- goal 序列化往返（API 存 dict，Worker 重建 Goal）
- 租约生命周期：begin → extend → release；过期接管（find_idle_runs）
- 终态不可再转移（冲突检测）
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ariadne.loop_module.goal import Assertion, AssertionKind, Budget, Goal
from ariadne.loop_module.state_machine import LoopState
from ariadne.storage.postgres.models import Base, Organization, Project
from ariadne.storage.postgres.repositories.loop_runs import (
    LoopRunConflictError,
    LoopRunNotFoundError,
    LoopRunRepository,
    goal_from_dict,
    to_row_dict,
)

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

# 共享内存：同一 engine 的所有会话共享一个库（Worker 跨会话验证用）
_ENGINE_URL = "sqlite+aiosqlite:///file:loop_runs_test?mode=memory&cache=shared&uri=true"


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(_ENGINE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as sess:
        sess.add(Organization(id=ORG_ID, name="test-org"))
        sess.add(Project(id=PROJECT_ID, org_id=ORG_ID, slug="t", name="Test"))
        await sess.commit()
        yield sess
    await engine.dispose()


def make_goal(mode: str = "quality") -> Goal:
    return Goal(
        task="生成产品发布公告",
        assertions=(
            Assertion(
                id="length",
                kind=AssertionKind.REGEX,
                spec={"pattern": r"^.{1,500}$"},
                hint="控制在 500 字以内",
            ),
            Assertion(
                id="quality",
                kind=AssertionKind.METRIC,
                spec={"name": "composite_quality", "op": ">=", "value": 80},
                blocking=False,
            ),
        ),
        budget=Budget(max_iterations=3, max_total_tokens=50_000, max_cost_usd=0.2),
        mode=mode,
    )


class TestGoalSerialization:
    async def test_create_and_roundtrip(self, session: AsyncSession) -> None:
        """create 存序列化 goal；goal_from_dict 重建出等价 Goal。"""
        repo = LoopRunRepository(session)
        goal = make_goal()
        loop_id = await repo.create(project_id=PROJECT_ID, mode="quality", goal=goal)

        row = await repo.get(project_id=PROJECT_ID, loop_id=loop_id)
        assert row.state == LoopState.VALIDATE.value
        assert row.mode == "quality"
        assert row.cumulative_tokens == 0

        # 重建（Worker 端）
        rebuilt = goal_from_dict(row.goal)
        assert rebuilt.task == goal.task
        assert len(rebuilt.assertions) == 2
        assert rebuilt.assertions[0].kind is AssertionKind.REGEX
        assert rebuilt.assertions[0].spec["pattern"] == r"^.{1,500}$"
        assert rebuilt.assertions[0].hint == "控制在 500 字以内"
        assert rebuilt.assertions[1].blocking is False
        assert rebuilt.budget.max_iterations == 3
        assert rebuilt.budget.max_cost_usd == 0.2
        assert rebuilt.mode == "quality"

    async def test_enum_kind_preserved(self, session: AsyncSession) -> None:
        """METRIC 断言 kind 字符串往返后仍还原为枚举。"""
        repo = LoopRunRepository(session)
        goal = make_goal()
        loop_id = await repo.create(project_id=PROJECT_ID, mode="quality", goal=goal)
        row = await repo.get(project_id=PROJECT_ID, loop_id=loop_id)

        rebuilt = goal_from_dict(row.goal)
        assert rebuilt.assertions[1].kind is AssertionKind.METRIC

    async def test_to_row_dict_shape(self, session: AsyncSession) -> None:
        """API 响应的字段形状。"""
        repo = LoopRunRepository(session)
        loop_id = await repo.create(project_id=PROJECT_ID, mode="quality", goal=make_goal())
        row = await repo.get(project_id=PROJECT_ID, loop_id=loop_id)
        d = to_row_dict(row)
        assert d["id"] == str(loop_id)
        assert d["state"] == "VALIDATE"
        assert d["final_state"] is None
        assert d["worker_id"] is None
        assert d["goal"]["task"] == "生成产品发布公告"


class TestLeaseLifecycle:
    async def test_begin_extend_release(self, session: AsyncSession) -> None:
        """租约全生命周期。"""
        repo = LoopRunRepository(session)
        loop_id = await repo.create(project_id=PROJECT_ID, mode="quality", goal=make_goal())

        begin = await repo.begin_lease(loop_id=loop_id, worker_id="w1", duration_seconds=60)
        assert begin.worker_id == "w1"
        assert begin.lease_expires_at is not None

        await repo.extend_lease(loop_id=loop_id, worker_id="w1", duration_seconds=60)
        await repo.release_lease(loop_id=loop_id, worker_id="w1")
        row = await repo.by_id(loop_id)
        assert row.worker_id is None
        assert row.lease_expires_at is None

    async def test_extend_with_wrong_worker_raises(self, session: AsyncSession) -> None:
        """持有者不符时续租失败 —— 防双 Worker 误接管。"""
        repo = LoopRunRepository(session)
        loop_id = await repo.create(project_id=PROJECT_ID, mode="quality", goal=make_goal())
        await repo.begin_lease(loop_id=loop_id, worker_id="w1", duration_seconds=60)
        with pytest.raises(LoopRunConflictError):
            await repo.extend_lease(loop_id=loop_id, worker_id="w2", duration_seconds=60)

    async def test_begin_lease_refuses_to_steal_live_lease(
        self, session: AsyncSession
    ) -> None:
        """租约未过期时第二个 Worker 抢不到。

        这是并发执行的唯一闸门：begin_lease 原先无条件覆盖 worker_id，
        调用方的"读行 → 查租约 → 写租约"是 check-then-act，两个 Worker
        同时通过检查就会并发跑同一个 Loop（各自续租、各自写检查点、
        各自落终态）。归属判定必须在 UPDATE 的 WHERE 里。
        """
        repo = LoopRunRepository(session)
        loop_id = await repo.create(project_id=PROJECT_ID, mode="quality", goal=make_goal())
        await repo.begin_lease(loop_id=loop_id, worker_id="w1", duration_seconds=60)

        with pytest.raises(LoopRunConflictError, match="w1"):
            await repo.begin_lease(loop_id=loop_id, worker_id="w2", duration_seconds=60)
        assert (await repo.by_id(loop_id)).worker_id == "w1"

    async def test_begin_lease_takes_over_expired_lease(
        self, session: AsyncSession
    ) -> None:
        """租约过期后可以接管 —— 否则崩溃的 Worker 会永久占住 Loop。

        SQLite 把 DateTime(timezone=True) 存成 naive，而 WHERE 里的比较值是
        aware。这条断言同时守着"过期判定在数据库侧真的成立"，不是只在
        Python 里成立。
        """
        repo = LoopRunRepository(session)
        loop_id = await repo.create(project_id=PROJECT_ID, mode="quality", goal=make_goal())
        await repo.begin_lease(loop_id=loop_id, worker_id="w1", duration_seconds=60)
        row = await repo.by_id(loop_id)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.flush()

        taken = await repo.begin_lease(
            loop_id=loop_id, worker_id="w2", duration_seconds=60
        )
        assert taken.worker_id == "w2"

    async def test_begin_lease_is_reentrant_for_same_worker(
        self, session: AsyncSession
    ) -> None:
        """同一 Worker 重复获取要成功：接管自己上一轮未完成的 Loop 是正常路径。"""
        repo = LoopRunRepository(session)
        loop_id = await repo.create(project_id=PROJECT_ID, mode="quality", goal=make_goal())
        await repo.begin_lease(loop_id=loop_id, worker_id="w1", duration_seconds=60)
        again = await repo.begin_lease(
            loop_id=loop_id, worker_id="w1", duration_seconds=60
        )
        assert again.worker_id == "w1"

    async def test_release_does_not_clear_another_workers_lease(
        self, session: AsyncSession
    ) -> None:
        """迟到的原持有者不能清掉接管者的租约。

        时序：w1 租约超时 → w2 接管 → w1 才走到清理。原实现无条件清空，
        于是 w2 正在执行的 Loop 变成"无人持有"，立刻被第三个 Worker 认领。
        """
        repo = LoopRunRepository(session)
        loop_id = await repo.create(project_id=PROJECT_ID, mode="quality", goal=make_goal())
        await repo.begin_lease(loop_id=loop_id, worker_id="w2", duration_seconds=60)

        await repo.release_lease(loop_id=loop_id, worker_id="w1")
        assert (await repo.by_id(loop_id)).worker_id == "w2"

    async def test_find_idle_expired_leases(self, session: AsyncSession) -> None:
        """过期租约可被接管（崩溃 Worker 的场景）。"""
        repo = LoopRunRepository(session)
        loop_id = await repo.create(project_id=PROJECT_ID, mode="quality", goal=make_goal())
        # 租约已过期
        await repo.begin_lease(loop_id=loop_id, worker_id="w1", duration_seconds=1)
        row = await repo.by_id(loop_id)
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=100)
        row.updated_at = datetime.now(UTC) - timedelta(seconds=2000)
        await session.flush()

        idle = await repo.find_idle_runs()
        assert any(r.id == loop_id for r in idle)

    async def test_find_idle_never_claimed(self, session: AsyncSession) -> None:
        """从未认领的活跃 Loop（如刚创建）可被接管。"""
        repo = LoopRunRepository(session)
        loop_id = await repo.create(project_id=PROJECT_ID, mode="quality", goal=make_goal())
        row = await repo.by_id(loop_id)
        row.updated_at = datetime.now(UTC) - timedelta(seconds=2000)
        await session.flush()

        idle = await repo.find_idle_runs()
        assert any(r.id == loop_id for r in idle)

    async def test_find_idle_excludes_final(self, session: AsyncSession) -> None:
        """已终态的 Loop 不该被接管。"""
        repo = LoopRunRepository(session)
        loop_id = await repo.create(project_id=PROJECT_ID, mode="quality", goal=make_goal())
        await repo.finish(
            loop_id=loop_id, final_state=LoopState.CONVERGED, worker_id="w1"
        )
        idle = await repo.find_idle_runs()
        assert not any(r.id == loop_id for r in idle)


class TestTerminalTransitions:
    async def test_finish_sets_terminal_state(self, session: AsyncSession) -> None:
        """finish 写 final_state + 状态 + 释放租约。"""
        repo = LoopRunRepository(session)
        loop_id = await repo.create(project_id=PROJECT_ID, mode="quality", goal=make_goal())
        await repo.begin_lease(loop_id=loop_id, worker_id="w1", duration_seconds=60)
        await repo.finish(
            loop_id=loop_id,
            final_state=LoopState.CONVERGED,
            error="",
            worker_id="w1",
        )
        row = await repo.by_id(loop_id)
        assert row.final_state == LoopState.CONVERGED.value
        assert row.state == LoopState.CONVERGED.value
        assert row.finished_at is not None
        assert row.worker_id is None

    async def test_transition_after_terminal_raises(self, session: AsyncSession) -> None:
        """终态不能转移。"""
        repo = LoopRunRepository(session)
        loop_id = await repo.create(project_id=PROJECT_ID, mode="quality", goal=make_goal())
        await repo.finish(
            loop_id=loop_id, final_state=LoopState.CONVERGED, worker_id="w1"
        )
        with pytest.raises(LoopRunConflictError):
            await repo.transition(loop_id=loop_id, to=LoopState.PLANNING)

    async def test_update_metrics(self, session: AsyncSession) -> None:
        """更新累计用量。"""
        repo = LoopRunRepository(session)
        loop_id = await repo.create(project_id=PROJECT_ID, mode="quality", goal=make_goal())
        await repo.update_metrics(loop_id=loop_id, iteration=3, tokens=1200, cost_usd=0.05)
        row = await repo.by_id(loop_id)
        assert row.iteration == 3
        assert row.cumulative_tokens == 1200
        assert float(row.cumulative_cost_usd) == pytest.approx(0.05)

    async def test_not_found(self, session: AsyncSession) -> None:
        repo = LoopRunRepository(session)
        with pytest.raises(LoopRunNotFoundError):
            await repo.by_id(uuid.UUID("00000000-0000-0000-0000-000000000999"))


class TestList:
    async def test_list_recent_filters(self, session: AsyncSession) -> None:
        repo = LoopRunRepository(session)
        g = make_goal()
        await repo.create(project_id=PROJECT_ID, mode="quality", goal=g)
        id2 = await repo.create(project_id=PROJECT_ID, mode="hitl", goal=make_goal("hitl"))

        rows = await repo.list_recent(project_id=PROJECT_ID, limit=10)
        assert len(rows) == 2
        rows_q = await repo.list_recent(project_id=PROJECT_ID, mode="hitl")
        assert len(rows_q) == 1
        assert rows_q[0].id == id2