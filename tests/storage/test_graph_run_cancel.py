"""GraphRunRepository 取消机制单元测试（阶段 3）。"""

import uuid
from datetime import UTC, datetime

import pytest

from ariadne.storage.postgres.repositories.graph_runs import GraphRunRepository


@pytest.fixture
async def graph_runs_repo(memory_pg) -> GraphRunRepository:
    """创建 GraphRunRepository 实例。"""
    async with memory_pg.session() as session:
        yield GraphRunRepository(session)


@pytest.fixture
async def default_project_id() -> uuid.UUID:
    """默认项目 ID。"""
    return uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
async def sample_graph_id() -> uuid.UUID:
    """示例图 ID。"""
    return uuid.UUID("00000000-0000-0000-0000-000000000002")


@pytest.mark.asyncio
async def test_cancel_pending_run(
    graph_runs_repo: GraphRunRepository,
    default_project_id: uuid.UUID,
    sample_graph_id: uuid.UUID,
) -> None:
    """测试取消 PENDING 状态的任务。"""
    # 创建 PENDING 任务
    run_id = await graph_runs_repo.create(
        project_id=default_project_id,
        graph_id=sample_graph_id,
        inputs={"test": "data"},
    )

    # 取消任务
    success = await graph_runs_repo.cancel(run_id)
    assert success is True

    # 验证 cancelled_at 被设置（SQLite 返回 naive datetime，需归一化再比较）
    run = await graph_runs_repo.by_id(run_id)
    assert run.cancelled_at is not None
    cancelled = run.cancelled_at
    if cancelled.tzinfo is None:
        cancelled = cancelled.replace(tzinfo=UTC)
    assert cancelled <= datetime.now(UTC)


@pytest.mark.asyncio
async def test_cancel_running_run(
    graph_runs_repo: GraphRunRepository,
    default_project_id: uuid.UUID,
    sample_graph_id: uuid.UUID,
) -> None:
    """测试取消 RUNNING 状态的任务。"""
    # 创建并认领任务（RUNNING）
    run_id = await graph_runs_repo.create(
        project_id=default_project_id,
        graph_id=sample_graph_id,
        inputs={"test": "data"},
    )
    await graph_runs_repo.begin_lease(run_id, "worker-1", lease_duration_s=300)

    # 取消任务
    success = await graph_runs_repo.cancel(run_id)
    assert success is True

    # 验证 cancelled_at 被设置
    run = await graph_runs_repo.by_id(run_id)
    assert run.cancelled_at is not None


@pytest.mark.asyncio
async def test_cancel_completed_run_fails(
    graph_runs_repo: GraphRunRepository,
    default_project_id: uuid.UUID,
    sample_graph_id: uuid.UUID,
) -> None:
    """测试取消已完成的任务（应失败）。"""
    # 创建并完成任务
    run_id = await graph_runs_repo.create(
        project_id=default_project_id,
        graph_id=sample_graph_id,
        inputs={"test": "data"},
    )
    await graph_runs_repo.finish(
        graph_run_id=run_id,
        final_state="COMPLETED",
        outputs={"result": "done"},
        node_states={},
        errors=[],
    )

    # 尝试取消已完成的任务
    success = await graph_runs_repo.cancel(run_id)
    assert success is False


@pytest.mark.asyncio
async def test_cancel_already_cancelled_fails(
    graph_runs_repo: GraphRunRepository,
    default_project_id: uuid.UUID,
    sample_graph_id: uuid.UUID,
) -> None:
    """测试重复取消同一任务（应失败）。"""
    # 创建任务
    run_id = await graph_runs_repo.create(
        project_id=default_project_id,
        graph_id=sample_graph_id,
        inputs={"test": "data"},
    )

    # 第一次取消（成功）
    success1 = await graph_runs_repo.cancel(run_id)
    assert success1 is True

    # 第二次取消（失败）
    success2 = await graph_runs_repo.cancel(run_id)
    assert success2 is False


@pytest.mark.asyncio
async def test_is_cancelled(
    graph_runs_repo: GraphRunRepository,
    default_project_id: uuid.UUID,
    sample_graph_id: uuid.UUID,
) -> None:
    """测试 is_cancelled 方法。"""
    # 创建任务
    run_id = await graph_runs_repo.create(
        project_id=default_project_id,
        graph_id=sample_graph_id,
        inputs={"test": "data"},
    )

    # 初始状态：未取消
    is_cancelled = await graph_runs_repo.is_cancelled(run_id)
    assert is_cancelled is False

    # 取消后：已取消
    await graph_runs_repo.cancel(run_id)
    is_cancelled = await graph_runs_repo.is_cancelled(run_id)
    assert is_cancelled is True
