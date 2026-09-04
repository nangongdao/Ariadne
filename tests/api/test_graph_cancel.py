"""Graph 取消 API 测试（阶段 3）。"""

import uuid

import pytest

from ariadne.graph_module.models import Edge, NodeBase, NodeKind, WorkflowGraph

# 使用测试常量
TEST_PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def sample_graph_id(client, auth: dict[str, str]) -> uuid.UUID:
    """创建示例图并返回其 ID。"""
    from ariadne.graph_module.models import Port, PortKind
    from ariadne.graph_module.serialize import graph_to_spec

    # 创建完整的、可校验通过的图
    graph = WorkflowGraph(
        nodes=(
            NodeBase(
                id="llm_node",
                kind=NodeKind.LLM,
                inputs=(Port(name="prompt", kind=PortKind.TEXT),),
                outputs=(Port(name="text", kind=PortKind.TEXT),),
                params={"prompt": "test prompt", "model": "gpt-4"},
            ),
            NodeBase(
                id="eval_node",
                kind=NodeKind.EVAL,
                inputs=(Port(name="text", kind=PortKind.TEXT),),
                outputs=(Port(name="result", kind=PortKind.JSON),),
                params={"assertions": [{"type": "contains", "value": "test"}]},
            ),
        ),
        edges=(
            Edge(source="llm_node", source_port="text", target="eval_node", target_port="text"),
        ),
    )

    # API 期望完整的 spec 格式（包含 graph 字段）
    full_spec = graph_to_spec(graph)

    response = client.post(
        "/v1/graphs",
        json={
            "name": "test-cancel-graph",
            "description": "Test graph for cancellation",
            "graph": full_spec,
        },
        headers=auth,
    )
    assert response.status_code == 201, f"创建图失败: {response.text}"
    return uuid.UUID(response.json()["id"])


@pytest.mark.asyncio
async def test_cancel_pending_graph_run(
    client,
    sample_graph_id: uuid.UUID,
    auth: dict[str, str],
) -> None:
    """测试取消 PENDING 状态的 graph run。"""
    # 1. 创建 graph run（不自动执行）
    from ariadne.storage.postgres.repositories.graph_runs import GraphRunRepository

    async with client.app.state.pg.session() as session:
        repo = GraphRunRepository(session)
        graph_run_id = await repo.create(
            project_id=TEST_PROJECT_ID,
            graph_id=sample_graph_id,
            inputs={"test": "data"},
        )
        await session.commit()

    # 2. 取消任务
    response = client.post(
        f"/v1/graphs/runs/{graph_run_id}/cancel",
        headers=auth,
    )
    assert response.status_code == 200
    data = response.json()
    assert "取消" in data["message"]

    # 3. 验证 cancelled_at 被设置
    response = client.get(
        f"/v1/graphs/runs/{graph_run_id}",
        headers=auth,
    )
    assert response.status_code == 200
    run = response.json()
    assert run["cancelled_at"] is not None


@pytest.mark.asyncio
async def test_cancel_nonexistent_graph_run(
    client,
    auth: dict[str, str],
) -> None:
    """测试取消不存在的 graph run。"""
    fake_id = uuid.uuid4()
    response = client.post(
        f"/v1/graphs/runs/{fake_id}/cancel",
        headers=auth,
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_cancel_completed_graph_run(
    client,
    sample_graph_id: uuid.UUID,
    auth: dict[str, str],
) -> None:
    """测试取消已完成的 graph run（应失败）。"""
    # 1. 创建并完成 graph run
    from ariadne.storage.postgres.repositories.graph_runs import GraphRunRepository

    async with client.app.state.pg.session() as session:
        repo = GraphRunRepository(session)
        graph_run_id = await repo.create(
            project_id=TEST_PROJECT_ID,
            graph_id=sample_graph_id,
            inputs={"test": "data"},
        )
        await repo.finish(
            graph_run_id=graph_run_id,
            final_state="COMPLETED",
            outputs={"result": "done"},
            node_states={},
            errors=[],
        )
        await session.commit()

    # 2. 尝试取消已完成的任务
    response = client.post(
        f"/v1/graphs/runs/{graph_run_id}/cancel",
        headers=auth,
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_cancel_already_cancelled(
    client,
    sample_graph_id: uuid.UUID,
    auth: dict[str, str],
) -> None:
    """测试重复取消同一任务。"""
    # 1. 创建 graph run
    from ariadne.storage.postgres.repositories.graph_runs import GraphRunRepository

    async with client.app.state.pg.session() as session:
        repo = GraphRunRepository(session)
        graph_run_id = await repo.create(
            project_id=TEST_PROJECT_ID,
            graph_id=sample_graph_id,
            inputs={"test": "data"},
        )
        await session.commit()

    # 2. 第一次取消（成功）
    response = client.post(
        f"/v1/graphs/runs/{graph_run_id}/cancel",
        headers=auth,
    )
    assert response.status_code == 200

    # 3. 第二次取消（失败）
    response = client.post(
        f"/v1/graphs/runs/{graph_run_id}/cancel",
        headers=auth,
    )
    assert response.status_code == 400
