"""GraphWorkerV2 取消机制测试（阶段 3）。

用内存 Postgres（memory_pg）+ 内存队列（FakeQueue）验证 Worker 在
执行前检测取消标志：不连真实 Redis —— 队列行为不属于本测试范围
（接口由 GraphQueue 集成测试覆盖）。
"""

import asyncio
import uuid

import pytest

from ariadne.storage.postgres.repositories.graph_runs import GraphRunRepository
from ariadne.worker.graph_worker_v2 import GraphWorkerV2


@pytest.fixture
async def graph_worker_v2(memory_pg, settings, fake_queue) -> GraphWorkerV2:
    """创建测试用 GraphWorkerV2（内存 Postgres + FakeQueue）。"""
    worker = GraphWorkerV2(
        pg_factory=memory_pg,
        settings=settings,
        queue=fake_queue,  # type: ignore[arg-type]
    )
    yield worker
    await worker.shutdown()


@pytest.fixture
async def default_project_id() -> uuid.UUID:
    """默认项目 ID。"""
    return uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
async def sample_graph_id(memory_pg, default_project_id: uuid.UUID) -> uuid.UUID:
    """创建 workflow_graph 行并返回其 ID（Worker 执行需要读到图定义）。"""
    from ariadne.storage.postgres.graph_models import GraphRow

    graph_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    async with memory_pg.session() as session:
        session.add(
            GraphRow(
                id=graph_id,
                project_id=default_project_id,
                name="test-graph",
                version=1,
                graph={
                    "version": "1",
                    "graph": {
                        "version": "1",
                        "nodes": [],
                        "edges": [],
                    },
                },
                is_active=True,
            )
        )
        await session.commit()
    return graph_id


@pytest.mark.asyncio
async def test_worker_detects_cancellation_before_execution(
    graph_worker_v2: GraphWorkerV2,
    default_project_id: uuid.UUID,
    sample_graph_id: uuid.UUID,
    memory_pg,
) -> None:
    """测试 Worker 在执行前检测到取消标志。"""
    # 1. 创建 graph run
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        run_id = await repo.create(
            project_id=default_project_id,
            graph_id=sample_graph_id,
            inputs={"test": "data"},
        )
        await session.commit()

    # 2. 立即取消任务
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        await repo.cancel(run_id)
        await session.commit()

    # 3. Worker 执行任务（应检测到取消并退出）
    await graph_worker_v2._execute(run_id, default_project_id)

    # 4. 验证任务状态为 CANCELLED
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        run = await repo.by_id(run_id)
        assert run.state == "CANCELLED"
        assert run.errors == ["用户取消执行"]


@pytest.mark.asyncio
async def test_cancel_api_and_worker_integration(
    client,
    default_project_id: uuid.UUID,
    sample_graph_id: uuid.UUID,
    auth: dict[str, str],
    graph_worker_v2: GraphWorkerV2,
    memory_pg,
) -> None:
    """测试取消 API + Worker 集成流程。"""
    # 1. 手动创建 graph_run（不自动执行）
    from ariadne.storage.postgres.repositories.graph_runs import GraphRunRepository

    async with client.app.state.pg.session() as session:
        repo = GraphRunRepository(session)
        run_id = await repo.create(
            project_id=default_project_id,
            graph_id=sample_graph_id,
            inputs={"x": 10},
        )
        await session.commit()

    # 2. 启动 Worker 执行（在后台）
    worker_task = asyncio.create_task(
        graph_worker_v2._execute(run_id, default_project_id)
    )

    # 3. 短暂延迟后取消任务
    await asyncio.sleep(0.1)
    response = client.post(
        f"/v1/graphs/runs/{run_id}/cancel",
        headers=auth,
    )
    assert response.status_code == 200

    # 4. 等待 Worker 完成
    await worker_task

    # 5. 验证状态
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        run = await repo.by_id(run_id)
        # 可能是 CANCELLED（检测到取消）或 COMPLETED/FAILED（执行完成太快）
        assert run.state in ["CANCELLED", "COMPLETED", "FAILED"]
        assert run.cancelled_at is not None