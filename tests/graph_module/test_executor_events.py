"""GraphExecutor 节点事件回调测试（阶段 3-3）。

验证 node_event_callback 在每个节点状态落定（completed/failed/skipped）
时被调用；回调失败不阻塞图执行（尽力而为语义）。
"""

from typing import Any

import pytest

from ariadne.graph_module.executor import (
    GraphExecutor,
    NodeExecutionContext,
    NodeState,
)
from ariadne.graph_module.models import Edge, NodeBase, NodeKind, WorkflowGraph


class DummyNodeExecutor:
    """返回固定输出。"""

    def __init__(self, output: dict[str, Any] | None = None) -> None:
        self.output = output or {"data": "ok"}

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        return self.output


class FailingNodeExecutor:
    """总是失败。"""

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        raise RuntimeError("intentional failure")


def _node(node_id: str, kind: NodeKind) -> NodeBase:
    return NodeBase(id=node_id, kind=kind, params={})


def _edge(src: str, tgt: str) -> Edge:
    return Edge(source=src, source_port="output", target=tgt, target_port="input")


class RecordingCallback:
    """记录 on_node_state 调用。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, NodeState]] = []

    async def on_node_state(self, node_id: str, state: NodeState) -> None:
        self.events.append((node_id, state))


@pytest.mark.asyncio
async def test_events_emitted_for_all_nodes() -> None:
    """线性图：每个节点落定都触发事件，按拓扑序。"""
    graph = WorkflowGraph(
        nodes=(
            _node("input", NodeKind.LLM),
            _node("transform", NodeKind.TOOL),
            _node("output", NodeKind.EVAL),
        ),
        edges=(
            _edge("input", "transform"),
            _edge("transform", "output"),
        ),
    )

    callback = RecordingCallback()
    executor = GraphExecutor()
    node_executors = {
        NodeKind.LLM: DummyNodeExecutor({"data": "a"}),
        NodeKind.TOOL: DummyNodeExecutor({"data": "b"}),
        NodeKind.EVAL: DummyNodeExecutor({"result": "c"}),
    }

    await executor.run(
        graph,
        inputs={"input": "x"},
        node_executors=node_executors,
        node_event_callback=callback,
    )

    assert len(callback.events) == 3
    assert ("input", NodeState.COMPLETED) in callback.events
    assert ("transform", NodeState.COMPLETED) in callback.events
    assert ("output", NodeState.COMPLETED) in callback.events
    order = [node_id for node_id, _ in callback.events]
    assert order == ["input", "transform", "output"]


@pytest.mark.asyncio
async def test_events_on_failure_and_skip() -> None:
    """节点失败：FAILED 事件 + 下游 SKIPPED 事件。"""
    graph = WorkflowGraph(
        nodes=(
            _node("input", NodeKind.LLM),
            _node("failing", NodeKind.TOOL),
            _node("output", NodeKind.EVAL),
        ),
        edges=(
            _edge("input", "failing"),
            _edge("failing", "output"),
        ),
    )

    callback = RecordingCallback()
    executor = GraphExecutor()
    node_executors = {
        NodeKind.LLM: DummyNodeExecutor({"data": "a"}),
        NodeKind.TOOL: FailingNodeExecutor(),
        NodeKind.EVAL: DummyNodeExecutor({"result": "c"}),
    }

    await executor.run(
        graph,
        inputs={"input": "x"},
        node_executors=node_executors,
        node_event_callback=callback,
    )

    events = dict(callback.events)
    assert events["input"] == NodeState.COMPLETED
    assert events["failing"] == NodeState.FAILED
    assert events["output"] == NodeState.SKIPPED


@pytest.mark.asyncio
async def test_callback_exception_does_not_block_execution() -> None:
    """回调抛异常不阻塞图执行（尽力而为语义）。"""

    class ThrowingCallback:
        async def on_node_state(self, node_id: str, state: NodeState) -> None:
            raise RuntimeError("callback failed")

    graph = WorkflowGraph(nodes=(_node("input", NodeKind.LLM),))
    executor = GraphExecutor()
    result = await executor.run(
        graph,
        inputs={"input": "x"},
        node_executors={NodeKind.LLM: DummyNodeExecutor({"data": "a"})},
        node_event_callback=ThrowingCallback(),
    )
    assert result.errors == []
    assert result.node_states["input"] == NodeState.COMPLETED


@pytest.mark.asyncio
async def test_no_callback_is_noop() -> None:
    """无回调时正常运行（向后兼容）。"""
    graph = WorkflowGraph(nodes=(_node("input", NodeKind.LLM),))
    executor = GraphExecutor()
    result = await executor.run(
        graph,
        inputs={"input": "x"},
        node_executors={NodeKind.LLM: DummyNodeExecutor({"data": "a"})},
    )
    assert result.errors == []
    assert result.node_states["input"] == NodeState.COMPLETED
