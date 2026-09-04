"""Graph 检查点机制测试（阶段 3-2）。"""

import uuid

import pytest

from ariadne.graph_module.checkpointing import (
    GraphRunCheckpointSaver,
    NoOpCheckpointSaver,
    load_checkpoint,
    load_checkpoint_outputs,
    should_skip_node,
)
from ariadne.graph_module.executor import NodeState
from ariadne.storage.postgres.repositories.graph_runs import GraphRunRepository


@pytest.mark.asyncio
async def test_noop_checkpoint_saver() -> None:
    """测试空检查点保存器（不执行任何操作）。"""
    saver = NoOpCheckpointSaver()
    # 不应抛出异常
    await saver.save("node1", NodeState.COMPLETED)
    await saver.save("node2", NodeState.FAILED)


@pytest.mark.asyncio
async def test_graph_run_checkpoint_saver(memory_pg) -> None:
    """测试 GraphRunCheckpointSaver 保存检查点到数据库。"""
    project_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    graph_id = uuid.UUID("00000000-0000-0000-0000-000000000002")

    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)

        # 创建 graph_run
        graph_run_id = await repo.create(
            project_id=project_id,
            graph_id=graph_id,
            inputs={"test": "data"},
        )
        await session.commit()

        # 创建检查点保存器
        saver = GraphRunCheckpointSaver(graph_run_id, repo)

        # 保存节点状态
        await saver.save("node1", NodeState.COMPLETED)
        await saver.save("node2", NodeState.COMPLETED)
        await saver.save("node3", NodeState.FAILED)

        # 验证检查点已保存
        checkpoint = await repo.get_checkpoint(graph_run_id)
        assert checkpoint is not None
        assert checkpoint["node1"] == "completed"
        assert checkpoint["node2"] == "completed"
        assert checkpoint["node3"] == "failed"

        # 验证 get_completed_nodes
        completed = saver.get_completed_nodes()
        assert completed == {"node1", "node2"}


@pytest.mark.asyncio
async def test_load_checkpoint_empty(memory_pg) -> None:
    """测试加载不存在的检查点（返回 None）。"""
    project_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    graph_id = uuid.UUID("00000000-0000-0000-0000-000000000002")

    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)

        # 创建 graph_run（无检查点）
        graph_run_id = await repo.create(
            project_id=project_id,
            graph_id=graph_id,
            inputs={"test": "data"},
        )
        await session.commit()

        # 加载检查点
        checkpoint = await load_checkpoint(graph_run_id, repo)
        assert checkpoint is None


@pytest.mark.asyncio
async def test_load_checkpoint_with_data(memory_pg) -> None:
    """测试加载已保存的检查点。"""
    project_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    graph_id = uuid.UUID("00000000-0000-0000-0000-000000000002")

    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)

        # 创建 graph_run
        graph_run_id = await repo.create(
            project_id=project_id,
            graph_id=graph_id,
            inputs={"test": "data"},
        )
        await session.commit()

        # 保存检查点
        checkpoint_data = {
            "node1": "completed",
            "node2": "completed",
            "node3": "failed",
        }
        await repo.save_checkpoint(graph_run_id, checkpoint_data)
        await session.commit()

        # 加载检查点
        checkpoint = await load_checkpoint(graph_run_id, repo)
        assert checkpoint == checkpoint_data


def test_should_skip_node() -> None:
    """测试 should_skip_node 判断逻辑。"""
    checkpoint = {
        "node1": "completed",
        "node2": "failed",
        "node3": "pending",
    }

    # completed 节点应跳过
    assert should_skip_node("node1", checkpoint) is True

    # failed 节点不跳过（需要重试）
    assert should_skip_node("node2", checkpoint) is False

    # pending 节点不跳过
    assert should_skip_node("node3", checkpoint) is False

    # 不在检查点中的节点不跳过
    assert should_skip_node("node4", checkpoint) is False

    # 无检查点时不跳过任何节点
    assert should_skip_node("node1", None) is False


@pytest.mark.asyncio
async def test_checkpoint_incremental_save(memory_pg) -> None:
    """测试检查点增量保存（每个节点完成后更新）。"""
    project_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    graph_id = uuid.UUID("00000000-0000-0000-0000-000000000002")

    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)

        # 创建 graph_run
        graph_run_id = await repo.create(
            project_id=project_id,
            graph_id=graph_id,
            inputs={"test": "data"},
        )
        await session.commit()

        saver = GraphRunCheckpointSaver(graph_run_id, repo)

        # 第一次保存
        await saver.save("node1", NodeState.COMPLETED)
        checkpoint = await repo.get_checkpoint(graph_run_id)
        assert checkpoint == {"node1": "completed"}

        # 第二次保存（增量）
        await saver.save("node2", NodeState.COMPLETED)
        checkpoint = await repo.get_checkpoint(graph_run_id)
        assert checkpoint == {"node1": "completed", "node2": "completed"}

        # 第三次保存（包含失败节点）
        await saver.save("node3", NodeState.FAILED)
        checkpoint = await repo.get_checkpoint(graph_run_id)
        assert checkpoint == {
            "node1": "completed",
            "node2": "completed",
            "node3": "failed",
        }


@pytest.mark.asyncio
async def test_checkpoint_persists_and_loads_node_outputs(memory_pg) -> None:
    """检查点必须同时保存状态和恢复数据流所需的节点输出。"""
    import uuid

    from ariadne.storage.postgres.repositories.graph_runs import GraphRunRepository

    project_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    graph_id = uuid.UUID("00000000-0000-0000-0000-000000000002")

    async with memory_pg.session() as session:
        repo = GraphRunRepository(session)
        run_id = await repo.create(project_id, graph_id, {})
        saver = GraphRunCheckpointSaver(run_id, repo)

        await saver.save_with_output(
            "source", NodeState.COMPLETED, {"output": "persisted"}
        )
        await saver.save_with_output(
            "consumer", NodeState.FAILED, None
        )

        assert await load_checkpoint(run_id, repo) == {
            "source": "completed",
            "consumer": "failed",
        }
        assert await load_checkpoint_outputs(run_id, repo) == {
            "source": {"output": "persisted"}
        }
