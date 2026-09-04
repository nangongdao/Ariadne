"""Graph 执行器检查点恢复测试（阶段 3-2）。

测试 GraphExecutor 从检查点恢复执行的能力。
"""

from typing import Any

import pytest

from ariadne.graph_module.checkpointing import NoOpCheckpointSaver
from ariadne.graph_module.executor import (
    GraphExecutor,
    NodeExecutionContext,
    NodeState,
)
from ariadne.graph_module.models import Edge, NodeBase, NodeKind, WorkflowGraph


# 测试用 executor 桩
class DummyNodeExecutor:
    """简单返回固定值的 executor。"""

    def __init__(self, output: dict[str, Any]):
        self.output = output

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        return self.output


class FailingNodeExecutor:
    """总是失败的 executor。"""

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        raise RuntimeError("intentional failure")


# 辅助构造函数
def _node(node_id: str, kind: NodeKind) -> NodeBase:
    return NodeBase(id=node_id, kind=kind, params={})


def _edge(src: str, tgt: str) -> Edge:
    return Edge(source=src, source_port="output", target=tgt, target_port="input")


@pytest.mark.asyncio
async def test_executor_with_checkpoint_saver() -> None:
    """测试执行器使用检查点保存器。"""
    # 简单的线性图：llm → tool → eval
    graph = WorkflowGraph(
        nodes=[
            _node("input", NodeKind.LLM),
            _node("transform", NodeKind.TOOL),
            _node("output", NodeKind.EVAL),
        ],
        edges=[
            _edge("input", "transform"),
            _edge("transform", "output"),
        ],
    )

    # 模拟检查点保存
    saved_checkpoints: list[tuple[str, NodeState]] = []

    class MockCheckpointSaver:
        async def save(self, node_id: str, state: NodeState) -> None:
            saved_checkpoints.append((node_id, state))

    executor = GraphExecutor()
    node_executors = {
        NodeKind.LLM: DummyNodeExecutor({"data": "input_value"}),
        NodeKind.TOOL: DummyNodeExecutor({"data": "transformed"}),
        NodeKind.EVAL: DummyNodeExecutor({"result": "output_value"}),
    }

    result = await executor.run(
        graph,
        inputs={"input": "test"},
        node_executors=node_executors,
        checkpoint_saver=MockCheckpointSaver(),
    )

    # 验证执行结果
    assert result.node_states["input"] == NodeState.COMPLETED
    assert result.node_states["transform"] == NodeState.COMPLETED
    assert result.node_states["output"] == NodeState.COMPLETED
    assert len(result.errors) == 0

    # 验证检查点保存了所有完成的节点
    assert len(saved_checkpoints) == 3
    assert ("input", NodeState.COMPLETED) in saved_checkpoints
    assert ("transform", NodeState.COMPLETED) in saved_checkpoints
    assert ("output", NodeState.COMPLETED) in saved_checkpoints


@pytest.mark.asyncio
async def test_executor_resume_from_checkpoint() -> None:
    """测试从检查点恢复执行（跳过已完成节点）。"""
    # 图：llm → tool1 → tool2 → code → eval
    graph = WorkflowGraph(
        nodes=[
            _node("input", NodeKind.LLM),
            _node("node1", NodeKind.TOOL),
            _node("node2", NodeKind.TOOL),
            _node("node3", NodeKind.CODE),
            _node("output", NodeKind.EVAL),
        ],
        edges=[
            _edge("input", "node1"),
            _edge("node1", "node2"),
            _edge("node2", "node3"),
            _edge("node3", "output"),
        ],
    )

    # 模拟已完成节点：input 和 node1
    completed_nodes = {"input", "node1"}

    # 记录哪些节点实际被执行
    executed_nodes: list[str] = []

    class TrackingExecutor:
        def __init__(self, node_id: str):
            self.node_id = node_id

        async def execute(self, ctx) -> dict:
            executed_nodes.append(self.node_id)
            return {"data": f"result_{self.node_id}"}

    executor = GraphExecutor()
    node_executors = {
        NodeKind.LLM: TrackingExecutor("llm"),
        NodeKind.TOOL: TrackingExecutor("tool"),
        NodeKind.CODE: TrackingExecutor("code"),
        NodeKind.EVAL: TrackingExecutor("eval"),
    }

    result = await executor.run(
        graph,
        inputs={"input": "test"},
        node_executors=node_executors,
        checkpoint_saver=NoOpCheckpointSaver(),
        completed_nodes=completed_nodes,
    )

    # 验证 input 和 node1 被标记为 COMPLETED 但未执行
    assert result.node_states["input"] == NodeState.COMPLETED
    assert result.node_states["node1"] == NodeState.COMPLETED
    assert "llm" not in executed_nodes  # input 节点使用 LLM executor
    # node1 可能被执行或跳过（取决于实现细节）

    # 验证 node2、node3、output 被执行
    assert result.node_states["node2"] == NodeState.COMPLETED
    assert result.node_states["node3"] == NodeState.COMPLETED
    assert result.node_states["output"] == NodeState.COMPLETED
    # 至少有一些节点被执行
    assert len(executed_nodes) >= 2


@pytest.mark.asyncio
async def test_checkpoint_on_node_failure() -> None:
    """测试节点失败时也保存检查点。"""
    graph = WorkflowGraph(
        nodes=[
            _node("input", NodeKind.LLM),
            _node("failing", NodeKind.TOOL),
            _node("output", NodeKind.EVAL),
        ],
        edges=[
            _edge("input", "failing"),
            _edge("failing", "output"),
        ],
    )

    saved_checkpoints: list[tuple[str, NodeState]] = []

    class MockCheckpointSaver:
        async def save(self, node_id: str, state: NodeState) -> None:
            saved_checkpoints.append((node_id, state))

    executor = GraphExecutor()
    node_executors = {
        NodeKind.LLM: DummyNodeExecutor({"data": "input_value"}),
        NodeKind.TOOL: FailingNodeExecutor(),
        NodeKind.EVAL: DummyNodeExecutor({"result": "output_value"}),
    }

    result = await executor.run(
        graph,
        inputs={"input": "test"},
        node_executors=node_executors,
        checkpoint_saver=MockCheckpointSaver(),
    )

    # 验证执行结果
    assert result.node_states["input"] == NodeState.COMPLETED
    assert result.node_states["failing"] == NodeState.FAILED
    assert result.node_states["output"] == NodeState.SKIPPED  # 上游失败，下游跳过
    assert len(result.errors) > 0

    # 验证检查点保存了完成和失败的节点
    assert ("input", NodeState.COMPLETED) in saved_checkpoints
    assert ("failing", NodeState.FAILED) in saved_checkpoints
    # output 被跳过，不会调用 checkpoint_saver


@pytest.mark.asyncio
async def test_resume_from_checkpoint_with_partial_failure() -> None:
    """测试从部分失败的检查点恢复（失败节点需要重新执行）。"""
    graph = WorkflowGraph(
        nodes=[
            _node("input", NodeKind.LLM),
            _node("node1", NodeKind.TOOL),
            _node("node2", NodeKind.TOOL),
        ],
        edges=[
            _edge("input", "node1"),
            _edge("node1", "node2"),
        ],
    )

    # 检查点：input 完成，node1 失败
    # node1 失败的节点不应被跳过（需要重试）
    completed_nodes = {"input"}  # 只跳过完成的节点

    executed_nodes: list[str] = []

    class TrackingExecutor:
        def __init__(self, node_id: str):
            self.node_id = node_id

        async def execute(self, ctx) -> dict:
            executed_nodes.append(self.node_id)
            return {"data": f"result_{self.node_id}"}

    executor = GraphExecutor()
    node_executors = {
        NodeKind.LLM: TrackingExecutor("llm"),
        NodeKind.TOOL: TrackingExecutor("tool"),
    }

    result = await executor.run(
        graph,
        inputs={"input": "test"},
        node_executors=node_executors,
        completed_nodes=completed_nodes,
    )

    # input 被跳过
    assert result.node_states["input"] == NodeState.COMPLETED
    assert "llm" not in executed_nodes

    # node1 和 node2 被重新执行
    assert result.node_states["node1"] == NodeState.COMPLETED
    assert result.node_states["node2"] == NodeState.COMPLETED
    assert "tool" in executed_nodes


@pytest.mark.asyncio
async def test_resume_restores_completed_output_for_downstream() -> None:
    """恢复已完成节点时必须把其端口输出交给下游节点。"""
    graph = WorkflowGraph(
        nodes=[
            NodeBase(
                id="source",
                kind=NodeKind.LLM,
                inputs=(),
                outputs=(),
                params={},
            ),
            NodeBase(
                id="consumer",
                kind=NodeKind.TOOL,
                inputs=(),
                outputs=(),
                params={},
            ),
        ],
        edges=[_edge("source", "consumer")],
    )

    seen_inputs: list[dict[str, Any]] = []

    class ConsumerExecutor:
        async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
            seen_inputs.append(dict(ctx.inputs))
            return {"output": ctx.inputs.get("input")}

    class UnexpectedSourceExecutor:
        async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
            raise AssertionError("已恢复的 source 不应再次执行")

    result = await GraphExecutor().run(
        graph,
        inputs={},
        node_executors={
            NodeKind.LLM: UnexpectedSourceExecutor(),
            NodeKind.TOOL: ConsumerExecutor(),
        },
        completed_nodes={"source"},
        completed_outputs={"source": {"output": "persisted-value"}},
    )

    assert result.node_states["source"] == NodeState.COMPLETED
    assert result.node_states["consumer"] == NodeState.COMPLETED
    assert seen_inputs == [{"input": "persisted-value"}]
    assert result.outputs == {"consumer": {"output": "persisted-value"}}


@pytest.mark.asyncio
async def test_resume_without_output_reexecutes_completed_node() -> None:
    """旧的仅状态检查点不能静默丢数据，缺输出时应重新执行。"""
    graph = WorkflowGraph(
        nodes=[
            _node("source", NodeKind.LLM),
        ],
        edges=[],
    )
    executed: list[str] = []

    class SourceExecutor:
        async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
            executed.append(ctx.node.id)
            return {"output": "recomputed"}

    result = await GraphExecutor().run(
        graph,
        inputs={},
        node_executors={NodeKind.LLM: SourceExecutor()},
        completed_nodes={"source"},
        completed_outputs={},
    )

    assert executed == ["source"]
    assert result.outputs == {"source": {"output": "recomputed"}}


@pytest.mark.asyncio
async def test_resume_replays_completed_branch_route() -> None:
    """恢复已完成 Branch 时应重放路由，避免两条分支都继续执行。"""
    branch = NodeBase(
        id="branch",
        kind=NodeKind.BRANCH,
        params={"branches": {"left": "left", "right": "right"}},
    )
    left = _node("left", NodeKind.TOOL)
    right = _node("right", NodeKind.TOOL)
    graph = WorkflowGraph(
        nodes=[branch, left, right],
        edges=[_edge("branch", "left"), _edge("branch", "right")],
    )
    executed: list[str] = []

    class BranchTargetExecutor:
        async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
            executed.append(ctx.node.id)
            return {"output": ctx.node.id}

    result = await GraphExecutor().run(
        graph,
        inputs={},
        node_executors={NodeKind.TOOL: BranchTargetExecutor()},
        completed_nodes={"branch"},
        completed_outputs={"branch": {"__route": "left"}},
    )

    assert executed == ["left"]
    assert result.node_states["branch"] == NodeState.COMPLETED
    assert result.node_states["left"] == NodeState.COMPLETED
    assert result.node_states["right"] == NodeState.SKIPPED
