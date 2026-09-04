"""GraphRunRepository 租约机制测试（阶段 2）。"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from ariadne.storage.postgres.repositories.graph_runs import GraphRunRepository


def _as_utc(dt: datetime) -> datetime:
    """SQLite 返回 naive datetime，归一化为 aware 再比较。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


@pytest.fixture
async def graph_runs_repo(memory_pg) -> GraphRunRepository:
    """创建 GraphRunRepository 实例（内存 Postgres 会话）。"""
    async with memory_pg.session() as session:
        yield GraphRunRepository(session)


@pytest.fixture
async def session_factory(memory_pg):
    """提供 memory_pg.session 上下文管理器函数（供多会话用例使用）。

    memory_pg.session 是 @asynccontextmanager：调用即进入新会话，退出时
    自动 commit —— 测试里 `async with session_factory() as session:` 拿到
    独立会话，互不共享事务。
    """
    return memory_pg.session


@pytest.fixture
async def default_project_id() -> uuid.UUID:
    """默认项目 ID。"""
    return uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
async def sample_graph_id() -> uuid.UUID:
    """示例图 ID。"""
    return uuid.UUID("00000000-0000-0000-0000-000000000002")


@pytest.mark.asyncio
async def test_begin_lease_success(
    graph_runs_repo: GraphRunRepository,
    sample_graph_run_id: uuid.UUID,
) -> None:
    """测试成功认领租约。"""
    success = await graph_runs_repo.begin_lease(
        sample_graph_run_id, "worker-1", lease_duration_s=300
    )
    assert success is True

    # 验证状态变为 RUNNING，设置了 worker_id 和 lease_expires_at
    run = await graph_runs_repo.by_id(sample_graph_run_id)
    assert run.state == "RUNNING"
    assert run.worker_id == "worker-1"
    assert run.lease_expires_at is not None
    assert _as_utc(run.lease_expires_at) > datetime.now(UTC)


@pytest.mark.asyncio
async def test_begin_lease_conflict(
    graph_runs_repo: GraphRunRepository,
    sample_graph_run_id: uuid.UUID,
) -> None:
    """测试租约冲突（多个 Worker 竞争）。"""
    # Worker-1 认领成功
    success1 = await graph_runs_repo.begin_lease(
        sample_graph_run_id, "worker-1", lease_duration_s=300
    )
    assert success1 is True

    # Worker-2 尝试认领同一任务（应该失败）
    success2 = await graph_runs_repo.begin_lease(
        sample_graph_run_id, "worker-2", lease_duration_s=300
    )
    assert success2 is False

    # 验证 worker_id 仍然是 worker-1
    run = await graph_runs_repo.by_id(sample_graph_run_id)
    assert run.worker_id == "worker-1"


@pytest.mark.asyncio
async def test_extend_lease(
    graph_runs_repo: GraphRunRepository,
    sample_graph_run_id: uuid.UUID,
) -> None:
    """测试续约。"""
    # 先认领
    await graph_runs_repo.begin_lease(
        sample_graph_run_id, "worker-1", lease_duration_s=60
    )

    run = await graph_runs_repo.by_id(sample_graph_run_id)
    old_expires_at = run.lease_expires_at
    assert old_expires_at is not None

    # 续约
    success = await graph_runs_repo.extend_lease(
        sample_graph_run_id, "worker-1", lease_duration_s=300
    )
    assert success is True

    # 验证 lease_expires_at 被延长
    run = await graph_runs_repo.by_id(sample_graph_run_id)
    assert run.lease_expires_at is not None
    assert _as_utc(run.lease_expires_at) > _as_utc(old_expires_at)


@pytest.mark.asyncio
async def test_extend_lease_wrong_worker(
    graph_runs_repo: GraphRunRepository,
    sample_graph_run_id: uuid.UUID,
) -> None:
    """测试错误的 Worker 无法续约。"""
    # Worker-1 认领
    await graph_runs_repo.begin_lease(
        sample_graph_run_id, "worker-1", lease_duration_s=300
    )

    # Worker-2 尝试续约（应该失败）
    success = await graph_runs_repo.extend_lease(
        sample_graph_run_id, "worker-2", lease_duration_s=300
    )
    assert success is False


@pytest.mark.asyncio
async def test_release_lease(
    graph_runs_repo: GraphRunRepository,
    sample_graph_run_id: uuid.UUID,
) -> None:
    """测试释放租约。"""
    # 先认领
    await graph_runs_repo.begin_lease(
        sample_graph_run_id, "worker-1", lease_duration_s=300
    )

    # 释放租约
    await graph_runs_repo.release_lease(sample_graph_run_id, "worker-1")

    # 验证 worker_id 和 lease_expires_at 被清空
    run = await graph_runs_repo.by_id(sample_graph_run_id)
    assert run.worker_id is None
    assert run.lease_expires_at is None


@pytest.mark.asyncio
async def test_find_expired_leases(
    graph_runs_repo: GraphRunRepository,
    session_factory,
    default_project_id: uuid.UUID,
    sample_graph_id: uuid.UUID,
) -> None:
    """测试查找过期租约。"""
    # 创建一个过期的租约（手动设置过去的时间）
    async with session_factory() as session:
        repo = GraphRunRepository(session)

        # 创建任务并设置过期租约
        run_id = await repo.create(
            project_id=default_project_id,
            graph_id=sample_graph_id,
            inputs={"test": "data"},
        )
        await session.commit()

        # 手动设置为过期状态
        await session.execute(
            text("""
                UPDATE graph_runs
                SET state = 'RUNNING',
                    worker_id = 'worker-crashed',
                    lease_expires_at = :expired_time
                WHERE id = :run_id
            """),
            {
                # SQLite 方言：UUID 不能直接绑定，需转 str；datetime 只能 naive
                "run_id": str(run_id),
                "expired_time": datetime.now(UTC).replace(tzinfo=None)
                - timedelta(minutes=10),
            },
        )
        await session.commit()

        # 查找过期租约
        expired = await repo.find_expired_leases(limit=50)
        assert len(expired) >= 1
        assert any(r.id == run_id for r in expired)


@pytest.mark.asyncio
async def test_reclaim_expired_lease(
    graph_runs_repo: GraphRunRepository,
    sample_graph_run_id: uuid.UUID,
    session_factory,
) -> None:
    """测试回收过期租约。"""
    # 先认领（在显式 commit 的会话中写，避免 SQLite 写锁）
    async with session_factory() as session:
        repo = GraphRunRepository(session)
        await repo.begin_lease(
            sample_graph_run_id, "worker-1", lease_duration_s=300
        )
        await session.commit()

    # 手动设置为过期
    async with session_factory() as session:
        await session.execute(
            text("""
                UPDATE graph_runs
                SET lease_expires_at = :expired_time
                WHERE id = :run_id
            """),
            {
                "run_id": str(sample_graph_run_id),
                "expired_time": datetime.now(UTC).replace(tzinfo=None)
                - timedelta(minutes=10),
            },
        )
        await session.commit()

    # Worker-2 回收过期租约
    success = await graph_runs_repo.reclaim_expired_lease(
        sample_graph_run_id, "worker-2", lease_duration_s=300
    )
    assert success is True

    # 验证 worker_id 变为 worker-2
    run = await graph_runs_repo.by_id(sample_graph_run_id)
    assert run.worker_id == "worker-2"
    assert run.lease_expires_at is not None
    assert _as_utc(run.lease_expires_at) > datetime.now(UTC)


@pytest.fixture
async def sample_graph_run_id(
    memory_pg,
    default_project_id: uuid.UUID,
    sample_graph_id: uuid.UUID,
) -> uuid.UUID:
    """创建示例 graph_run（PENDING 状态）并落库。

    用 memory_pg.session() 上下文直接创建：退出时自动 commit，不遗留
    未提交会话 —— 否则后续测试会话 UPDATE 同一行会触发 SQLite
    database is locked。
    """
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        run_id = await repo.create(
            project_id=default_project_id,
            graph_id=sample_graph_id,
            inputs={"test": "data"},
        )
    return run_id
