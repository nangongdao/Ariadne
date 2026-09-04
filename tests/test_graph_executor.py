"""graph_module 执行器测试 —— 拓扑排序、并发调度、条件分支、失败传播。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ariadne.graph_module.executor import (
    ROUTE_KEY,
    ExecutionResult,
    GraphExecutor,
    NodeExecutionContext,
    NodeExecutor,
    NodeState,
)
from ariadne.graph_module.models import (
    BRANCH_INPUTS,
    BRANCH_OUTPUTS,
    LLM_INPUTS,
    LLM_OUTPUTS,
    Edge,
    NodeBase,
    NodeKind,
    Port,
    PortKind,
    WorkflowGraph,
)

# ---------- 测试用桩 executor ----------


class StubExecutor(NodeExecutor):
    """记录执行顺序的桩 executor。"""

    def __init__(self, output_value: str = "result"):
        self.output_value = output_value
        self.executed: list[str] = []

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        self.executed.append(ctx.node.id)
        return {"text": f"{self.output_value}:{ctx.node.id}"}


class DelayedExecutor(NodeExecutor):
    """带延迟的桩，用于验证并发。"""

    def __init__(self, delay: float, marker: list[str], node_id: str):
        self.delay = delay
        self.marker = marker
        self.node_id = node_id

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        await asyncio.sleep(self.delay)
        self.marker.append(self.node_id)
        return {"text": f"done:{self.node_id}"}


class FailingExecutor(NodeExecutor):
    """总是失败的桩。"""

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        raise RuntimeError(f"boom:{ctx.node.id}")


class BranchExecutor(NodeExecutor):
    """根据 params["__route_value"] 返回路由。"""

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        route = ctx.node.params.get("__route_value", "default")
        return {ROUTE_KEY: route}


class CollectingExecutor(NodeExecutor):
    """收集输入并返回，用于验证数据传递。"""

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        return {"text": str(ctx.inputs)}


# ---------- 辅助构造函数 ----------


def _llm(node_id: str, **params: object) -> NodeBase:
    return NodeBase(
        id=node_id,
        kind=NodeKind.LLM,
        inputs=LLM_INPUTS,
        outputs=LLM_OUTPUTS,
        params={"prompt": "test", "model": "gpt-4", **params},
    )


def _branch(node_id: str, branches: dict[str, str], route_value: str = "default") -> NodeBase:
    return NodeBase(
        id=node_id,
        kind=NodeKind.BRANCH,
        inputs=BRANCH_INPUTS,
        outputs=BRANCH_OUTPUTS,
        params={"condition": "true", "branches": branches, "__route_value": route_value},
    )


def _sink(node_id: str) -> NodeBase:
    """无出边的终端节点。"""
    return NodeBase(
        id=node_id,
        kind=NodeKind.LLM,
        inputs=LLM_INPUTS,
        outputs=LLM_OUTPUTS,
        params={"prompt": "sink", "model": "gpt-4"},
    )


def _edge(src: str, src_port: str, tgt: str, tgt_port: str) -> Edge:
    return Edge(source=src, source_port=src_port, target=tgt, target_port=tgt_port)


# ======================================================================
# 线性图
# ======================================================================


class TestLinearExecution:
    """线性图 A→B→C 执行顺序。"""

    @pytest.mark.asyncio
    async def test_linear_execution_order(self):
        """A→B→C 按拓扑序执行。"""
        a = _llm("a")
        b = _llm("b")
        c = _llm("c")
        graph = WorkflowGraph(
            nodes=(a, b, c),
            edges=(
                _edge("a", "text", "b", "prompt"),
                _edge("b", "text", "c", "prompt"),
            ),
        )
        executor_impl = StubExecutor()
        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "start"},
            node_executors={NodeKind.LLM: executor_impl},
        )
        assert result.errors == []
        assert executor_impl.executed == ["a", "b", "c"]
        assert result.node_states["a"] == NodeState.COMPLETED
        assert result.node_states["b"] == NodeState.COMPLETED
        assert result.node_states["c"] == NodeState.COMPLETED

    @pytest.mark.asyncio
    async def test_linear_data_passing(self):
        """上游输出传递到下游输入。"""
        a = _llm("a")
        b = _llm("b")
        graph = WorkflowGraph(
            nodes=(a, b),
            edges=(_edge("a", "text", "b", "prompt"),),
        )

        class PassThroughExecutor(NodeExecutor):
            async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
                inp = ctx.inputs.get("prompt", "")
                return {"text": f"{inp}->processed"}

        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "start"},
            node_executors={NodeKind.LLM: PassThroughExecutor()},
        )
        assert result.errors == []
        assert result.outputs["b"]["text"] == "start->processed->processed"

    @pytest.mark.asyncio
    async def test_single_node(self):
        """单节点图执行。"""
        a = _llm("a")
        graph = WorkflowGraph(nodes=(a,))
        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "hello"},
            node_executors={NodeKind.LLM: StubExecutor()},
        )
        assert result.errors == []
        assert result.node_states["a"] == NodeState.COMPLETED
        assert "a" in result.outputs


# ======================================================================
# 并发执行
# ======================================================================


class TestConcurrentExecution:
    """同层节点并发执行。"""

    @pytest.mark.asyncio
    async def test_parallel_branches_concurrent(self):
        """A→B, A→C: B 和 C 应并发（总耗时 ≈ max(delay_b, delay_c)）。"""
        a = _llm("a")
        b = _llm("b")
        c = _llm("c")
        graph = WorkflowGraph(
            nodes=(a, b, c),
            edges=(
                _edge("a", "text", "b", "prompt"),
                _edge("a", "text", "c", "prompt"),
            ),
        )

        marker: list[str] = []
        executors = {
            NodeKind.LLM: _MultiNodeExecutor({
                "a": DelayedExecutor(0.01, marker, "a"),
                "b": DelayedExecutor(0.05, marker, "b"),
                "c": DelayedExecutor(0.05, marker, "c"),
            }),
        }

        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "start"},
            node_executors=executors,
        )
        assert result.errors == []
        # a 先执行，b 和 c 并发
        assert marker[0] == "a"
        assert set(marker[1:3]) == {"b", "c"}

    @pytest.mark.asyncio
    async def test_diamond_convergence(self):
        """菱形 A→B, A→C, B→D, C→D: B+C 并发，D 等两者完成后执行。"""
        a = _llm("a")
        b = _llm("b")
        c = _llm("c")
        d = _llm("d")
        graph = WorkflowGraph(
            nodes=(a, b, c, d),
            edges=(
                _edge("a", "text", "b", "prompt"),
                _edge("a", "text", "c", "prompt"),
                _edge("b", "text", "d", "prompt"),
                _edge("c", "text", "d", "prompt"),
            ),
        )

        marker: list[str] = []
        executors = {
            NodeKind.LLM: _MultiNodeExecutor({
                "a": DelayedExecutor(0.01, marker, "a"),
                "b": DelayedExecutor(0.03, marker, "b"),
                "c": DelayedExecutor(0.03, marker, "c"),
                "d": DelayedExecutor(0.01, marker, "d"),
            }),
        }

        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "start"},
            node_executors=executors,
        )
        assert result.errors == []
        assert result.node_states["d"] == NodeState.COMPLETED
        # d 在 b 和 c 之后执行
        assert marker.index("d") > marker.index("b")
        assert marker.index("d") > marker.index("c")

    @pytest.mark.asyncio
    async def test_concurrent_timing(self):
        """两个 0.05s 延迟的并发节点总耗时 < 0.09s（证明并发而非串行）。"""
        import time

        a = _llm("a")
        b = _llm("b")
        c = _llm("c")
        graph = WorkflowGraph(
            nodes=(a, b, c),
            edges=(
                _edge("a", "text", "b", "prompt"),
                _edge("a", "text", "c", "prompt"),
            ),
        )

        marker: list[str] = []
        executors = {
            NodeKind.LLM: _MultiNodeExecutor({
                "a": DelayedExecutor(0.01, marker, "a"),
                "b": DelayedExecutor(0.05, marker, "b"),
                "c": DelayedExecutor(0.05, marker, "c"),
            }),
        }

        start = time.monotonic()
        await GraphExecutor().run(
            graph,
            inputs={"prompt": "start"},
            node_executors=executors,
        )
        elapsed = time.monotonic() - start
        # 串行需要 0.01 + 0.05 + 0.05 = 0.11s；并发需要 0.01 + 0.05 = 0.06s。
        # 上限 0.105：留 45ms 余量吸收调度抖动（CI 负载下实测可到 ~35ms），
        # 同时串行下限 0.11s（sleep 不会提前返回）仍必然超限。
        assert elapsed < 0.105, f"并发执行失败: 耗时 {elapsed:.3f}s"


# ======================================================================
# 条件分支
# ======================================================================


class TestBranchRouting:
    """Branch 节点路由。"""

    @pytest.mark.asyncio
    async def test_branch_routes_to_selected(self):
        """Branch 路由到选中的分支，其他分支被 skip。"""
        branch_node = _branch("br", {"path_a": "a", "path_b": "b"}, route_value="path_a")
        a = _sink("a")
        b = _sink("b")
        graph = WorkflowGraph(
            nodes=(branch_node, a, b),
            edges=(
                _edge("br", "route", "a", "prompt"),
                _edge("br", "route", "b", "prompt"),
            ),
        )

        marker: list[str] = []
        executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.BRANCH: BranchExecutor(),
            NodeKind.LLM: _RecordingExecutor(marker),
        }

        result = await GraphExecutor().run(
            graph,
            inputs={},
            node_executors=executors,
        )
        assert result.errors == []
        assert result.node_states["br"] == NodeState.COMPLETED
        assert result.node_states["a"] == NodeState.COMPLETED
        assert result.node_states["b"] == NodeState.SKIPPED
        assert "a" in marker
        assert "b" not in marker

    @pytest.mark.asyncio
    async def test_branch_routes_to_other(self):
        """路由到 path_b 时 path_a 被 skip。"""
        branch_node = _branch("br", {"path_a": "a", "path_b": "b"}, route_value="path_b")
        a = _sink("a")
        b = _sink("b")
        graph = WorkflowGraph(
            nodes=(branch_node, a, b),
            edges=(
                _edge("br", "route", "a", "prompt"),
                _edge("br", "route", "b", "prompt"),
            ),
        )

        marker: list[str] = []
        executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.BRANCH: BranchExecutor(),
            NodeKind.LLM: _RecordingExecutor(marker),
        }

        result = await GraphExecutor().run(
            graph,
            inputs={},
            node_executors=executors,
        )
        assert result.errors == []
        assert result.node_states["a"] == NodeState.SKIPPED
        assert result.node_states["b"] == NodeState.COMPLETED

    @pytest.mark.asyncio
    async def test_branch_no_route_key(self):
        """Branch 无 __route 时不 skip 任何分支。"""
        branch_node = _branch("br", {"path_a": "a"}, route_value="path_a")
        a = _sink("a")
        graph = WorkflowGraph(
            nodes=(branch_node, a),
            edges=(_edge("br", "route", "a", "prompt"),),
        )

        class NoRouteBranch(NodeExecutor):
            async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
                return {}  # 无 ROUTE_KEY

        executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.BRANCH: NoRouteBranch(),
            NodeKind.LLM: StubExecutor(),
        }

        result = await GraphExecutor().run(
            graph,
            inputs={},
            node_executors=executors,
        )
        assert result.errors == []
        assert result.node_states["a"] == NodeState.COMPLETED

    @pytest.mark.asyncio
    async def test_branch_downstream_cascade_skip(self):
        """分支未选中的下游级联 skip。"""
        # br → a → a2（a 未选中，a2 也被 skip）
        branch_node = _branch("br", {"path_a": "a"}, route_value="nonexistent")
        a = _sink("a")
        a2 = _sink("a2")
        graph = WorkflowGraph(
            nodes=(branch_node, a, a2),
            edges=(
                _edge("br", "route", "a", "prompt"),
                _edge("a", "text", "a2", "prompt"),
            ),
        )

        executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.BRANCH: BranchExecutor(),
            NodeKind.LLM: StubExecutor(),
        }

        result = await GraphExecutor().run(
            graph,
            inputs={},
            node_executors=executors,
        )
        assert result.errors == []
        assert result.node_states["a"] == NodeState.SKIPPED
        assert result.node_states["a2"] == NodeState.SKIPPED


# ======================================================================
# 失败传播
# ======================================================================


class TestFailurePropagation:
    """节点失败 → 下游 skip。"""

    @pytest.mark.asyncio
    async def test_node_failure_skips_downstream(self):
        """B 失败 → C 被 skip。"""
        a = _llm("a")
        b = _llm("b")
        c = _llm("c")
        graph = WorkflowGraph(
            nodes=(a, b, c),
            edges=(
                _edge("a", "text", "b", "prompt"),
                _edge("b", "text", "c", "prompt"),
            ),
        )

        executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.LLM: _MultiNodeExecutor({
                "a": StubExecutor(),
                "b": FailingExecutor(),
                "c": StubExecutor(),
            }),
        }

        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "start"},
            node_executors=executors,
        )
        assert result.node_states["a"] == NodeState.COMPLETED
        assert result.node_states["b"] == NodeState.FAILED
        assert result.node_states["c"] == NodeState.SKIPPED
        assert len(result.errors) == 1
        assert "b" in result.errors[0]

    @pytest.mark.asyncio
    async def test_failure_in_parallel_branch(self):
        """并行分支中一个失败不影响另一个。"""
        a = _llm("a")
        b = _llm("b")
        c = _llm("c")
        d = _llm("d")
        graph = WorkflowGraph(
            nodes=(a, b, c, d),
            edges=(
                _edge("a", "text", "b", "prompt"),
                _edge("a", "text", "c", "prompt"),
                _edge("b", "text", "d", "prompt"),
            ),
        )

        executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.LLM: _MultiNodeExecutor({
                "a": StubExecutor(),
                "b": FailingExecutor(),
                "c": StubExecutor(),
                "d": StubExecutor(),
            }),
        }

        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "start"},
            node_executors=executors,
        )
        assert result.node_states["a"] == NodeState.COMPLETED
        assert result.node_states["b"] == NodeState.FAILED
        assert result.node_states["c"] == NodeState.COMPLETED
        assert result.node_states["d"] == NodeState.SKIPPED
        assert len(result.errors) == 1

    @pytest.mark.asyncio
    async def test_missing_executor_fails_node(self):
        """未注册 executor 的节点被标记 failed。"""
        a = _llm("a")
        graph = WorkflowGraph(nodes=(a,))
        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "start"},
            node_executors={},  # 无 LLM executor
        )
        assert result.node_states["a"] == NodeState.FAILED
        assert len(result.errors) == 1


# ======================================================================
# 空图和边角情况
# ======================================================================


class TestEdgeCases:
    """空图、环图、初始输入。"""

    @pytest.mark.asyncio
    async def test_empty_graph(self):
        """空图返回空结果。"""
        graph = WorkflowGraph()
        result = await GraphExecutor().run(
            graph,
            inputs={},
            node_executors={},
        )
        assert result.outputs == {}
        assert result.node_states == {}
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_cycle_graph_rejected(self):
        """环图被防御性拒绝。"""
        a = _llm("a")
        b = _llm("b")
        graph = WorkflowGraph(
            nodes=(a, b),
            edges=(
                _edge("a", "text", "b", "prompt"),
                _edge("b", "text", "a", "prompt"),
            ),
        )
        result = await GraphExecutor().run(
            graph,
            inputs={},
            node_executors={NodeKind.LLM: StubExecutor()},
        )
        assert len(result.errors) == 1
        assert "环" in result.errors[0]

    @pytest.mark.asyncio
    async def test_source_node_initial_inputs(self):
        """源节点从 inputs 参数获取初始输入。"""
        a = _llm("a")
        graph = WorkflowGraph(nodes=(a,))

        class CaptureExecutor(NodeExecutor):
            async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
                return {"text": ctx.inputs.get("prompt", "missing")}

        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "initial_value"},
            node_executors={NodeKind.LLM: CaptureExecutor()},
        )
        assert result.outputs["a"]["text"] == "initial_value"

    @pytest.mark.asyncio
    async def test_terminal_outputs_only(self):
        """只有终端节点（无出边）出现在 outputs 中。"""
        a = _llm("a")
        b = _llm("b")
        graph = WorkflowGraph(
            nodes=(a, b),
            edges=(_edge("a", "text", "b", "prompt"),),
        )
        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "start"},
            node_executors={NodeKind.LLM: StubExecutor()},
        )
        assert "b" in result.outputs
        assert "a" not in result.outputs

    @pytest.mark.asyncio
    async def test_all_nodes_completed_on_success(self):
        """成功执行后所有节点状态为 completed。"""
        a = _llm("a")
        b = _llm("b")
        c = _llm("c")
        graph = WorkflowGraph(
            nodes=(a, b, c),
            edges=(
                _edge("a", "text", "b", "prompt"),
                _edge("a", "text", "c", "prompt"),
            ),
        )
        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "start"},
            node_executors={NodeKind.LLM: StubExecutor()},
        )
        assert all(s == NodeState.COMPLETED for s in result.node_states.values())

    @pytest.mark.asyncio
    async def test_inputs_none_defaults_to_empty(self):
        """inputs=None 等价于空 dict。"""
        a = _llm("a")
        graph = WorkflowGraph(nodes=(a,))
        result = await GraphExecutor().run(
            graph,
            inputs=None,
            node_executors={NodeKind.LLM: StubExecutor()},
        )
        assert result.node_states["a"] == NodeState.COMPLETED


# ======================================================================
# 多输入端口
# ======================================================================


class TestMultipleInputs:
    """节点有多个输入端口时数据正确映射。"""

    @pytest.mark.asyncio
    async def test_two_upstreams_merge_into_one_node(self):
        """两个上游分别连到同一节点的两个不同输入端口。"""
        a = NodeBase(
            id="a", kind=NodeKind.LLM, inputs=(), outputs=(Port(name="text", kind=PortKind.TEXT),),
            params={"prompt": "x", "model": "m"},
        )
        b = NodeBase(
            id="b", kind=NodeKind.LLM, inputs=(), outputs=(Port(name="text", kind=PortKind.TEXT),),
            params={"prompt": "x", "model": "m"},
        )
        # c 有两个输入端口
        c = NodeBase(
            id="c", kind=NodeKind.LLM,
            inputs=(
                Port(name="prompt", kind=PortKind.TEXT),
                Port(name="context", kind=PortKind.TEXT),
            ),
            outputs=(Port(name="text", kind=PortKind.TEXT),),
            params={"prompt": "x", "model": "m"},
        )
        graph = WorkflowGraph(
            nodes=(a, b, c),
            edges=(
                _edge("a", "text", "c", "prompt"),
                _edge("b", "text", "c", "context"),
            ),
        )

        class MergeExecutor(NodeExecutor):
            async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
                return {"text": f"{ctx.inputs.get('prompt','')}+{ctx.inputs.get('context','')}"}

        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "init"},
            node_executors={
                NodeKind.LLM: _MultiNodeExecutor({
                    "a": StubExecutor(),
                    "b": StubExecutor(),
                    "c": MergeExecutor(),
                }),
            },
        )
        assert result.errors == []
        # a 输出 "result:a", b 输出 "result:b"
        # c 输入: prompt="result:a", context="result:b"
        assert result.outputs["c"]["text"] == "result:a+result:b"


# ======================================================================
# 深层级联
# ======================================================================


class TestDeepCascade:
    """深层图的级联 skip 和执行。"""

    @pytest.mark.asyncio
    async def test_deep_chain_execution(self):
        """5 节点链式图全部执行。"""
        nodes = tuple(_llm(f"n{i}") for i in range(5))
        edges = tuple(
            _edge(f"n{i}", "text", f"n{i+1}", "prompt") for i in range(4)
        )
        graph = WorkflowGraph(nodes=nodes, edges=edges)
        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "start"},
            node_executors={NodeKind.LLM: StubExecutor()},
        )
        assert result.errors == []
        for i in range(5):
            assert result.node_states[f"n{i}"] == NodeState.COMPLETED

    @pytest.mark.asyncio
    async def test_deep_chain_failure_cascades(self):
        """链中第 3 个节点失败 → 后续全部 skip。"""
        nodes = tuple(_llm(f"n{i}") for i in range(5))
        edges = tuple(
            _edge(f"n{i}", "text", f"n{i+1}", "prompt") for i in range(4)
        )
        graph = WorkflowGraph(nodes=nodes, edges=edges)
        executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.LLM: _MultiNodeExecutor({
                f"n{i}": StubExecutor() if i != 2 else FailingExecutor()
                for i in range(5)
            }),
        }
        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "start"},
            node_executors=executors,
        )
        assert result.node_states["n0"] == NodeState.COMPLETED
        assert result.node_states["n1"] == NodeState.COMPLETED
        assert result.node_states["n2"] == NodeState.FAILED
        assert result.node_states["n3"] == NodeState.SKIPPED
        assert result.node_states["n4"] == NodeState.SKIPPED

    @pytest.mark.asyncio
    async def test_branch_then_linear_chain(self):
        """Branch 选中后接线性链全执行。"""
        br = _branch("br", {"go": "a"}, route_value="go")
        a = _sink("a")
        b = _sink("b")
        graph = WorkflowGraph(
            nodes=(br, a, b),
            edges=(
                _edge("br", "route", "a", "prompt"),
                _edge("a", "text", "b", "prompt"),
            ),
        )
        marker: list[str] = []
        executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.BRANCH: BranchExecutor(),
            NodeKind.LLM: _RecordingExecutor(marker),
        }
        result = await GraphExecutor().run(
            graph,
            inputs={},
            node_executors=executors,
        )
        assert result.errors == []
        assert marker == ["a", "b"]


# ======================================================================
# ExecutionResult 结构
# ======================================================================


class TestExecutionResult:
    """ExecutionResult 数据结构。"""

    def test_result_fields(self):
        """ExecutionResult 有 outputs/node_states/errors 三个字段。"""
        r = ExecutionResult()
        assert r.outputs == {}
        assert r.node_states == {}
        assert r.errors == []

    def test_result_with_data(self):
        """可以构造带数据的 ExecutionResult。"""
        r = ExecutionResult(
            outputs={"a": {"text": "hello"}},
            node_states={"a": NodeState.COMPLETED},
            errors=["something"],
        )
        assert r.outputs["a"]["text"] == "hello"
        assert r.node_states["a"] == NodeState.COMPLETED
        assert r.errors == ["something"]

    def test_node_state_enum_values(self):
        """NodeState 枚举值正确。"""
        assert NodeState.PENDING == "pending"
        assert NodeState.RUNNING == "running"
        assert NodeState.COMPLETED == "completed"
        assert NodeState.SKIPPED == "skipped"
        assert NodeState.FAILED == "failed"

    def test_route_key_constant(self):
        """ROUTE_KEY 是 __route。"""
        assert ROUTE_KEY == "__route"

    @pytest.mark.asyncio
    async def test_node_execution_context_fields(self):
        """NodeExecutionContext 包含 node/inputs/graph。"""
        a = _llm("a")
        graph = WorkflowGraph(nodes=(a,))

        class InspectExecutor(NodeExecutor):
            async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
                assert ctx.node.id == "a"
                assert ctx.inputs == {"prompt": "hello"}
                assert ctx.graph is not None
                assert ctx.graph.node_ids == frozenset({"a"})
                return {"text": "ok"}

        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "hello"},
            node_executors={NodeKind.LLM: InspectExecutor()},
        )
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_execution_context_default_inputs(self):
        """NodeExecutionContext 默认 inputs 为空 dict。"""
        ctx = NodeExecutionContext(node=_llm("x"))
        assert ctx.inputs == {}
        assert ctx.graph is None

    @pytest.mark.asyncio
    async def test_multiple_terminal_nodes(self):
        """多个终端节点都出现在 outputs 中。"""
        a = _llm("a")
        b = _llm("b")
        graph = WorkflowGraph(nodes=(a, b))  # 两个无连接的源节点
        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "start"},
            node_executors={NodeKind.LLM: StubExecutor()},
        )
        assert "a" in result.outputs
        assert "b" in result.outputs


# ======================================================================
# 辅助 executor 类
# ======================================================================


class _MultiNodeExecutor(NodeExecutor):
    """按节点 id 分发到不同子 executor。"""

    def __init__(self, by_id: dict[str, NodeExecutor]):
        self.by_id = by_id

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        sub = self.by_id.get(ctx.node.id)
        if sub is None:
            raise RuntimeError(f"no executor for {ctx.node.id}")
        return await sub.execute(ctx)


class _RecordingExecutor(NodeExecutor):
    """记录被执行的节点 id。"""

    def __init__(self, marker: list[str]):
        self.marker = marker

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        self.marker.append(ctx.node.id)
        return {"text": f"done:{ctx.node.id}"}
