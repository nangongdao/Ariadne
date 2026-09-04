"""GraphRunRepository 测试。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from ariadne.storage.postgres.repositories.graph_runs import (
    GraphRunNotFoundError,
    GraphRunRepository,
)

# 使用 conftest.py 中的 TEST_PROJECT
TEST_PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
TEST_GRAPH_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


@pytest.mark.asyncio
async def test_create_and_get(memory_pg: Any) -> None:
    """测试创建和查询 graph_run。"""
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)

        # 创建
        graph_run_id = await repo.create(
            project_id=TEST_PROJECT_ID,
            graph_id=TEST_GRAPH_ID,
            inputs={"query": "test"},
        )
        await session.commit()

    assert graph_run_id is not None

    # 查询
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        run = await repo.get(graph_run_id, TEST_PROJECT_ID)

        assert run.id == graph_run_id
        assert run.project_id == TEST_PROJECT_ID
        assert run.graph_id == TEST_GRAPH_ID
        assert run.state == "PENDING"
        assert run.inputs == {"query": "test"}
        assert run.outputs is None
        assert run.node_states is None
        assert run.errors is None
        assert run.created_at is not None
        assert run.finished_at is None


@pytest.mark.asyncio
async def test_get_not_found(memory_pg: Any) -> None:
    """测试查询不存在的 graph_run。"""
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)

        with pytest.raises(GraphRunNotFoundError):
            await repo.get(uuid.uuid4(), TEST_PROJECT_ID)


@pytest.mark.asyncio
async def test_transition(memory_pg: Any) -> None:
    """测试状态转移。"""
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)

        # 创建
        graph_run_id = await repo.create(
            project_id=TEST_PROJECT_ID,
            graph_id=TEST_GRAPH_ID,
            inputs={},
        )
        await session.commit()

    # PENDING → RUNNING
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        await repo.transition(graph_run_id, "RUNNING")
        await session.commit()

    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        run = await repo.by_id(graph_run_id)
        assert run.state == "RUNNING"

    # RUNNING → COMPLETED
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        await repo.transition(graph_run_id, "COMPLETED")
        await session.commit()

    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        run = await repo.by_id(graph_run_id)
        assert run.state == "COMPLETED"


@pytest.mark.asyncio
async def test_finish(memory_pg: Any) -> None:
    """测试标记终态。"""
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)

        # 创建
        graph_run_id = await repo.create(
            project_id=TEST_PROJECT_ID,
            graph_id=TEST_GRAPH_ID,
            inputs={"x": 1},
        )
        await session.commit()

    # 标记 COMPLETED
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        await repo.finish(
            graph_run_id=graph_run_id,
            final_state="COMPLETED",
            outputs={"result": 42},
            node_states={"node1": "completed", "node2": "completed"},
            errors=[],
        )
        await session.commit()

    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        run = await repo.by_id(graph_run_id)

        assert run.state == "COMPLETED"
        assert run.outputs == {"result": 42}
        assert run.node_states == {"node1": "completed", "node2": "completed"}
        assert run.errors == []
        assert run.finished_at is not None


@pytest.mark.asyncio
async def test_finish_with_errors(memory_pg: Any) -> None:
    """测试标记失败状态。"""
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)

        graph_run_id = await repo.create(
            project_id=TEST_PROJECT_ID,
            graph_id=TEST_GRAPH_ID,
            inputs={},
        )
        await session.commit()

    # 标记 FAILED
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        await repo.finish(
            graph_run_id=graph_run_id,
            final_state="FAILED",
            outputs=None,
            node_states={"node1": "completed", "node2": "failed"},
            errors=["Node node2 failed: division by zero"],
        )
        await session.commit()

    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        run = await repo.by_id(graph_run_id)

        assert run.state == "FAILED"
        assert run.outputs is None
        assert run.node_states == {"node1": "completed", "node2": "failed"}
        assert len(run.errors) == 1
        assert "division by zero" in run.errors[0]


@pytest.mark.asyncio
async def test_list_runs(memory_pg: Any) -> None:
    """测试列出 graph runs。"""
    # 创建 3 个 runs
    ids = []
    for i in range(3):
        async with memory_pg.session() as session:
            repo = GraphRunRepository(session)
            run_id = await repo.create(
                project_id=TEST_PROJECT_ID,
                graph_id=TEST_GRAPH_ID,
                inputs={"index": i},
            )
            ids.append(run_id)
            await session.commit()

    # 列出全部
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        runs = await repo.list_runs(TEST_PROJECT_ID, limit=10, offset=0)

        assert len(runs) == 3
        # 按创建时间倒序
        assert runs[0].id == ids[2]
        assert runs[1].id == ids[1]
        assert runs[2].id == ids[0]

    # 分页
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        runs_page1 = await repo.list_runs(TEST_PROJECT_ID, limit=2, offset=0)
        assert len(runs_page1) == 2
        assert runs_page1[0].id == ids[2]

    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        runs_page2 = await repo.list_runs(TEST_PROJECT_ID, limit=2, offset=2)
        assert len(runs_page2) == 1
        assert runs_page2[0].id == ids[0]


@pytest.mark.asyncio
async def test_list_runs_empty(memory_pg: Any) -> None:
    """测试空列表。"""
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        runs = await repo.list_runs(TEST_PROJECT_ID, limit=10, offset=0)
        assert runs == []


@pytest.mark.asyncio
async def test_by_id(memory_pg: Any) -> None:
    """测试 by_id（不限定 project_id，Worker 用）。"""
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)

        graph_run_id = await repo.create(
            project_id=TEST_PROJECT_ID,
            graph_id=TEST_GRAPH_ID,
            inputs={},
        )
        await session.commit()

    # Worker 用 by_id 查询（不需要 project_id）
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        run = await repo.by_id(graph_run_id)

        assert run.id == graph_run_id
        assert run.project_id == TEST_PROJECT_ID


@pytest.mark.asyncio
async def test_by_id_not_found(memory_pg: Any) -> None:
    """测试 by_id 不存在。"""
    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)

        with pytest.raises(GraphRunNotFoundError):
            await repo.by_id(uuid.uuid4())
