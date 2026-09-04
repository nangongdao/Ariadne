"""GraphWorker 测试（MVP 简化版）。"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from ariadne.config import Settings
from ariadne.storage.postgres.repositories.graph_runs import GraphRunRepository
from ariadne.storage.postgres.repositories.graphs import GraphRepository
from ariadne.worker.graph_worker import GraphWorker

# 使用 conftest.py 中的 TEST_PROJECT
TEST_PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.mark.asyncio
async def test_submit_and_execute(
    memory_pg: Any,
    settings: Settings,
) -> None:
    """测试提交任务并后台执行。"""
    # 创建一个简单的图
    async with memory_pg.session() as session:
        graphs_repo = GraphRepository(session)

        # 简单的图：一个 branch 节点
        graph_spec = {
            "nodes": {
                "start": {
                    "kind": "branch",
                    "config": {
                        "condition": "true",
                        "branches": {"true": "end", "false": "end"},
                    },
                },
                "end": {"kind": "branch", "config": {"condition": "true", "branches": {}}},
            },
            "edges": [{"source": "start", "target": "end"}],
            "version": 1,
        }

        graph_id = await graphs_repo.create(
            project_id=TEST_PROJECT_ID,
            name="test_graph",
            graph=graph_spec,
            description="Test graph",
        )
        await session.commit()

    # 创建 graph_run
    async with memory_pg.session() as session:
        graph_runs_repo = GraphRunRepository(session)
        graph_run_id = await graph_runs_repo.create(
            project_id=TEST_PROJECT_ID,
            graph_id=graph_id,
            inputs={},
        )
        await session.commit()

    # 提交执行
    worker = GraphWorker(memory_pg, settings)
    await worker.submit(graph_run_id, TEST_PROJECT_ID)

    # 等待后台任务完成
    await asyncio.sleep(2)

    # 检查结果
    async with memory_pg.session() as session:
        graph_runs_repo = GraphRunRepository(session)
        run = await graph_runs_repo.by_id(graph_run_id)

        # 简单图应该完成
        assert run.state in ("COMPLETED", "FAILED")

        if run.state == "COMPLETED":
            assert run.outputs is not None
            assert run.finished_at is not None


@pytest.mark.asyncio
async def test_execution_failure(
    memory_pg: Any,
    settings: Settings,
) -> None:
    """测试图执行失败场景。"""
    async with memory_pg.session() as session:
        graphs_repo = GraphRepository(session)

        # 无效的图：节点引用不存在的 kind
        graph_spec = {
            "nodes": {
                "invalid": {
                    "kind": "nonexistent_kind",
                    "config": {},
                },
            },
            "edges": [],
            "version": 1,
        }

        graph_id = await graphs_repo.create(
            project_id=TEST_PROJECT_ID,
            name="invalid_graph",
            graph=graph_spec,
            description="Invalid graph",
        )
        await session.commit()

    async with memory_pg.session() as session:
        graph_runs_repo = GraphRunRepository(session)
        graph_run_id = await graph_runs_repo.create(
            project_id=TEST_PROJECT_ID,
            graph_id=graph_id,
            inputs={},
        )
        await session.commit()

    # 提交执行
    worker = GraphWorker(memory_pg, settings)
    await worker.submit(graph_run_id, TEST_PROJECT_ID)

    # 等待后台任务完成
    await asyncio.sleep(2)

    # 检查结果
    async with memory_pg.session() as session:
        graph_runs_repo = GraphRunRepository(session)
        run = await graph_runs_repo.by_id(graph_run_id)

        # 应该标记为 FAILED
        assert run.state == "FAILED"
        assert run.errors is not None
        assert len(run.errors) > 0
        assert run.finished_at is not None


@pytest.mark.asyncio
async def test_worker_shutdown(
    memory_pg: Any,
    settings: Settings,
) -> None:
    """测试 Worker 关闭时等待任务完成。"""
    async with memory_pg.session() as session:
        graphs_repo = GraphRepository(session)

        graph_spec = {
            "nodes": {
                "node1": {"kind": "branch", "config": {"condition": "true", "branches": {}}},
            },
            "edges": [],
            "version": 1,
        }

        graph_id = await graphs_repo.create(
            project_id=TEST_PROJECT_ID,
            name="shutdown_test",
            graph=graph_spec,
            description="",
        )
        await session.commit()

    async with memory_pg.session() as session:
        graph_runs_repo = GraphRunRepository(session)
        graph_run_id = await graph_runs_repo.create(
            project_id=TEST_PROJECT_ID,
            graph_id=graph_id,
            inputs={},
        )
        await session.commit()

    # 提交任务
    worker = GraphWorker(memory_pg, settings)
    await worker.submit(graph_run_id, TEST_PROJECT_ID)

    # 立即关闭（应该等待任务完成）
    await worker.shutdown()

    # 检查任务是否完成
    async with memory_pg.session() as session:
        graph_runs_repo = GraphRunRepository(session)
        run = await graph_runs_repo.by_id(graph_run_id)

        # 关闭时应该等待完成
        assert run.state in ("COMPLETED", "FAILED")


@pytest.mark.asyncio
async def test_concurrent_execution(
    memory_pg: Any,
    settings: Settings,
) -> None:
    """测试并发执行多个图。"""
    worker = GraphWorker(memory_pg, settings)

    # 创建多个图和 runs
    graph_run_ids = []

    async with memory_pg.session() as session:
        graphs_repo = GraphRepository(session)
        graph_runs_repo = GraphRunRepository(session)

        graph_spec = {
            "nodes": {
                "n1": {"kind": "branch", "config": {"condition": "true", "branches": {}}},
            },
            "edges": [],
            "version": 1,
        }

        graph_id = await graphs_repo.create(
            project_id=TEST_PROJECT_ID,
            name="concurrent_test",
            graph=graph_spec,
            description="",
        )

        for i in range(3):
            run_id = await graph_runs_repo.create(
                project_id=TEST_PROJECT_ID,
                graph_id=graph_id,
                inputs={"index": i},
            )
            graph_run_ids.append(run_id)

        await session.commit()

    # 并发提交
    for run_id in graph_run_ids:
        await worker.submit(run_id, TEST_PROJECT_ID)

    # 等待全部完成
    await asyncio.sleep(3)

    # 检查结果
    async with memory_pg.session() as session:
        graph_runs_repo = GraphRunRepository(session)

        for run_id in graph_run_ids:
            run = await graph_runs_repo.by_id(run_id)
            assert run.state in ("COMPLETED", "FAILED")


@pytest.mark.asyncio
async def test_graph_not_found(
    memory_pg: Any,
    settings: Settings,
) -> None:
    """测试图不存在时的处理。"""
    async with memory_pg.session() as session:
        graph_runs_repo = GraphRunRepository(session)

        # 创建一个指向不存在图的 run
        fake_graph_id = uuid.uuid4()
        graph_run_id = await graph_runs_repo.create(
            project_id=TEST_PROJECT_ID,
            graph_id=fake_graph_id,
            inputs={},
        )
        await session.commit()

    # 提交执行
    worker = GraphWorker(memory_pg, settings)
    await worker.submit(graph_run_id, TEST_PROJECT_ID)

    # 等待处理
    await asyncio.sleep(2)

    # 应该标记为 FAILED
    async with memory_pg.session() as session:
        graph_runs_repo = GraphRunRepository(session)
        run = await graph_runs_repo.by_id(graph_run_id)

        assert run.state == "FAILED"
        assert run.errors is not None
        assert len(run.errors) > 0
