"""Loop 补偿扫描测试（审计 P0-3 / R12 第五例）。

入队失败（Redis 故障）的 Loop 已落库但不在任何队列里，XREAD 恢复不了
从未写入的消息 —— 此前没有任何机制救它们，Loop 永远停在 VALIDATE。

_reconcile 是第三层保障：扫描"非终态 + 无有效租约 + 超时无进展"的行
重新入队。测试盯三件事：
1. 卡住的 Loop 真的会被重新入队（而不是只写日志）
2. HUMAN_PENDING / 终态 / 租约未过期 / 刚更新的行不会被误伤
3. 入队失败时行保持原状，下一轮扫描自然重试
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ariadne.loop_module.goal import Assertion, AssertionKind, Budget, Goal
from ariadne.loop_module.state_machine import LoopState
from ariadne.storage.postgres.models import Base, Organization, Project
from ariadne.storage.postgres.repositories.loop_runs import LoopRunRepository
from ariadne.worker.loop_worker import LoopWorker

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
PROJECT_ID_2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000aa")

_ENGINE_URL = "sqlite+aiosqlite:///file:loop_reconcile_test?mode=memory&cache=shared&uri=true"


@dataclass
class RecordingQueue:
    """记录 enqueue 调用的假队列；可注入故障。"""

    fail: bool = False
    enqueued: list[tuple[str, str]] = field(default_factory=list)

    async def connect(self) -> None: ...
    async def ensure_group(self) -> None: ...

    async def enqueue(self, loop_id: str, project_id: str) -> str:
        if self.fail:
            raise ConnectionError("redis down")
        self.enqueued.append((loop_id, project_id))
        return "0-0"

    async def close(self) -> None: ...


class SharedMemoryPg:
    """PostgresStore 同接口的内存实现（与 test_loop_worker 同构）。"""

    def __init__(self, maker: Any) -> None:
        self._maker = maker

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self._maker() as s:
            try:
                yield s
                await s.commit()
            except Exception:
                await s.rollback()
                raise

    async def ping(self) -> bool:
        return True

    async def close(self) -> None: ...


def make_goal() -> Goal:
    return Goal(
        task="任务",
        assertions=(
            Assertion(
                id="has_def",
                kind=AssertionKind.REGEX,
                spec={"pattern": "def"},
            ),
        ),
        budget=Budget(max_iterations=5, max_total_tokens=50_000),
        mode="quality",
    )


def make_worker(maker: Any, queue: RecordingQueue, **kwargs: Any) -> LoopWorker:
    overrides = kwargs.get("worker_overrides", {})

    class _WorkerSettings:
        postgres = object()  # SharedMemoryPg 工厂不读它，仅占位
        worker = type(
            "W",
            (),
            {
                "reconcile_interval_seconds": 60.0,
                "reconcile_idle_seconds": 300,
                **overrides,
            },
        )()

    return LoopWorker(
        settings=_WorkerSettings(),  # type: ignore[arg-type]
        llm=None,
        pg_factory=lambda _settings: SharedMemoryPg(maker),
        counter_factory=lambda _url: None,
        queue=queue,  # type: ignore[arg-type]
    )


async def seed_project(maker: Any) -> None:
    async with maker() as sess:
        existing = await sess.execute(
            Project.__table__.select().where(Project.id == PROJECT_ID)
        )
        if existing.first() is None:
            sess.add(Organization(id=ORG_ID, name="org"))
            sess.add(Project(id=PROJECT_ID, org_id=ORG_ID, slug="a", name="A"))
            sess.add(Project(id=PROJECT_ID_2, org_id=ORG_ID, slug="b", name="B"))
            await sess.commit()


async def create_stuck_run(
    maker: Any,
    *,
    project_id: uuid.UUID,
    state: str = LoopState.VALIDATE.value,
    age_seconds: int = 600,
    lease_worker: str | None = None,
) -> uuid.UUID:
    """造一条"卡住"的运行行：非终态、无租约、updated_at 早于阈值。"""
    async with maker() as sess:
        repo = LoopRunRepository(sess)
        loop_id = await repo.create(project_id=project_id, mode="quality", goal=make_goal())
        row = await repo.by_id(loop_id)
        row.state = state
        row.final_state = None
        row.worker_id = lease_worker
        # lease_worker 非空时给一个**已过期**的租约（崩溃 Worker 场景）
        if lease_worker is not None:
            row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=100)
        row.updated_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
        await sess.commit()
    return loop_id


@pytest.fixture
async def pg_setup() -> AsyncIterator[tuple[Any, Any]]:
    engine = create_async_engine(_ENGINE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    await seed_project(maker)
    yield engine, maker
    await engine.dispose()


class TestReconcile:
    async def test_stuck_run_gets_reenqueued(self, pg_setup: Any) -> None:
        """入队失败停在 VALIDATE 的 Loop 被补偿扫描重新入队。"""
        _engine, maker = pg_setup
        loop_id = await create_stuck_run(maker, project_id=PROJECT_ID)
        queue = RecordingQueue()
        worker = make_worker(maker, queue)
        await worker._reconcile()
        assert (str(loop_id), str(PROJECT_ID)) in queue.enqueued
        assert worker._stats["reconciled"] == 1

    async def test_scans_all_projects(self, pg_setup: Any) -> None:
        """跨租户扫描：两个项目里卡住的 Loop 都被捞出来。"""
        _engine, maker = pg_setup
        loop_a = await create_stuck_run(maker, project_id=PROJECT_ID)
        loop_b = await create_stuck_run(maker, project_id=PROJECT_ID_2)
        queue = RecordingQueue()
        worker = make_worker(maker, queue)
        await worker._reconcile()
        enqueued = {loop_id for loop_id, _pid in queue.enqueued}
        assert {str(loop_a), str(loop_b)} <= enqueued

    async def test_expires_human_pending(self, pg_setup: Any) -> None:
        """HUMAN_PENDING 在等人工审批，重新入队是无效往返。"""
        _engine, maker = pg_setup
        await create_stuck_run(
            maker, project_id=PROJECT_ID, state=LoopState.HUMAN_PENDING.value
        )
        queue = RecordingQueue()
        worker = make_worker(maker, queue)
        await worker._reconcile()
        assert queue.enqueued == []

    async def test_excludes_fresh_rows(self, pg_setup: Any) -> None:
        """刚创建/刚被认领的行不动：阈值内无进展不等于卡住。"""
        _engine, maker = pg_setup
        await create_stuck_run(maker, project_id=PROJECT_ID, age_seconds=10)
        queue = RecordingQueue()
        worker = make_worker(maker, queue)
        await worker._reconcile()
        assert queue.enqueued == []

    async def test_excludes_active_lease(self, pg_setup: Any) -> None:
        """租约未过期的行有 Worker 在跑，不重新入队。"""
        _engine, maker = pg_setup
        async with maker() as sess:
            repo = LoopRunRepository(sess)
            loop_id = await repo.create(
                project_id=PROJECT_ID, mode="quality", goal=make_goal()
            )
            await repo.begin_lease(
                loop_id=loop_id, worker_id="w1", duration_seconds=600
            )
            row = await repo.by_id(loop_id)
            row.updated_at = datetime.now(UTC) - timedelta(seconds=600)
            await sess.commit()

        queue = RecordingQueue()
        worker = make_worker(maker, queue)
        await worker._reconcile()
        assert queue.enqueued == []

    async def test_requeues_expired_lease(self, pg_setup: Any) -> None:
        """租约已过期的行（Worker 崩溃 + 条目丢失）也会被捞回。"""
        _engine, maker = pg_setup
        loop_id = await create_stuck_run(
            maker, project_id=PROJECT_ID, lease_worker="dead-worker"
        )
        queue = RecordingQueue()
        worker = make_worker(maker, queue)
        await worker._reconcile()
        assert (str(loop_id), str(PROJECT_ID)) in queue.enqueued

    async def test_enqueue_failure_keeps_row_for_next_pass(self, pg_setup: Any) -> None:
        """入队失败不丢：行保持原状，下一轮扫描重试。"""
        _engine, maker = pg_setup
        loop_id = await create_stuck_run(maker, project_id=PROJECT_ID)
        queue = RecordingQueue(fail=True)
        worker = make_worker(maker, queue)
        await worker._reconcile()
        assert worker._stats["reconciled"] == 0

        queue.fail = False
        await worker._reconcile()
        assert (str(loop_id), str(PROJECT_ID)) in queue.enqueued

    async def test_terminal_runs_never_reenqueued(self, pg_setup: Any) -> None:
        """终态行不在扫描范围。"""
        _engine, maker = pg_setup
        async with maker() as sess:
            repo = LoopRunRepository(sess)
            loop_id = await repo.create(
                project_id=PROJECT_ID, mode="quality", goal=make_goal()
            )
            row = await repo.by_id(loop_id)
            row.updated_at = datetime.now(UTC) - timedelta(seconds=600)
            await repo.finish(
                loop_id=loop_id, final_state=LoopState.CONVERGED, worker_id="w1"
            )
            await sess.commit()

        queue = RecordingQueue()
        worker = make_worker(maker, queue)
        await worker._reconcile()
        assert queue.enqueued == []
