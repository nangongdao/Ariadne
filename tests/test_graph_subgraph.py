"""SUBGRAPH 节点测试 —— 嵌套子图执行 + 递归校验 + 序列化 round-trip。

D7 多 Agent 编排：一个 SUBGRAPH 节点内部运行独立子图，支持任意深度嵌套。
测试覆盖：
- 端口定义与注册
- 执行器：基本执行、缺参、子图失败、外部输入透传、多终端输出、嵌套、深度限制、空子图
- 校验：缺参、有效子图、子图内环/重复 id/类型不兼容/缺参、嵌套深度、反序列化失败、含 Loop
- 序列化：round-trip、嵌套 round-trip、YAML、空子图、含 Loop、复杂图、幂等
- 图执行集成：SUBGRAPH 在完整图中执行、数据流、失败级联、与 Branch 组合、并发
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ariadne.graph_module import NodeFactory, available_node_kinds
from ariadne.graph_module.executor import (
    GraphExecutor,
    NodeExecutionContext,
    NodeExecutor,
    NodeState,
)
from ariadne.graph_module.models import (
    LLM_INPUTS,
    LLM_OUTPUTS,
    NODE_INPUT_PORTS,
    NODE_OUTPUT_PORTS,
    NODE_REQUIRED_PARAMS,
    SUBGRAPH_INPUTS,
    SUBGRAPH_OUTPUTS,
    Edge,
    NodeBase,
    NodeKind,
    Port,
    PortKind,
    WorkflowGraph,
)
from ariadne.graph_module.nodes.subgraph import (
    MAX_SUBGRAPH_DEPTH,
    SubgraphNodeExecutor,
    SubgraphNodeParams,
)
from ariadne.graph_module.serialize import (
    _serialize_graph,
    graph_to_spec,
    graph_to_spec_yaml,
    spec_to_graph,
    spec_yaml_to_graph,
)
from ariadne.graph_module.validate import validate_graph

# ---------- 辅助构造函数 ----------


def _llm(node_id: str, **params: object) -> NodeBase:
    return NodeBase(
        id=node_id,
        kind=NodeKind.LLM,
        inputs=LLM_INPUTS,
        outputs=LLM_OUTPUTS,
        params={"prompt": "test", "model": "gpt-4", **params},
    )


def _edge(src: str, src_port: str, tgt: str, tgt_port: str) -> Edge:
    return Edge(source=src, source_port=src_port, target=tgt, target_port=tgt_port)


def _subgraph_node(
    node_id: str = "sub_0",
    graph: dict[str, Any] | WorkflowGraph | None = None,
) -> NodeBase:
    if graph is None:
        graph = {"version": "1", "nodes": [], "edges": []}
    if isinstance(graph, WorkflowGraph):
        graph = _serialize_graph(graph)
    return NodeBase(
        id=node_id,
        kind=NodeKind.SUBGRAPH,
        inputs=SUBGRAPH_INPUTS,
        outputs=SUBGRAPH_OUTPUTS,
        params={"graph": graph},
    )


def _simple_subgraph(output_text: str = "hello") -> dict[str, Any]:
    """单 LLM 节点的子图 dict。"""
    return {
        "version": "1",
        "nodes": [
            {
                "id": "inner_llm",
                "kind": "llm",
                "inputs": [{"name": "prompt", "kind": "text", "required": True}],
                "outputs": [{"name": "text", "kind": "text", "required": True}],
                "params": {"prompt": "test", "model": "gpt-4"},
            }
        ],
        "edges": [],
    }


def _two_node_subgraph() -> dict[str, Any]:
    """两节点子图：LLM → CODE。"""
    return {
        "version": "1",
        "nodes": [
            {
                "id": "n1",
                "kind": "llm",
                "inputs": [{"name": "prompt", "kind": "text", "required": True}],
                "outputs": [{"name": "text", "kind": "text", "required": True}],
                "params": {"prompt": "hello", "model": "gpt-4"},
            },
            {
                "id": "n2",
                "kind": "code",
                "inputs": [{"name": "input", "kind": "any", "required": False}],
                "outputs": [{"name": "result", "kind": "text", "required": True}],
                "params": {"code": "print(__input)"},
            },
        ],
        "edges": [
            {"source": "n1", "source_port": "text", "target": "n2", "target_port": "input"},
        ],
    }


def _cyclic_subgraph() -> dict[str, Any]:
    """有环的子图（a→b→a）。"""
    return {
        "version": "1",
        "nodes": [
            {
                "id": "a",
                "kind": "llm",
                "inputs": [{"name": "prompt", "kind": "text", "required": True}],
                "outputs": [{"name": "text", "kind": "text", "required": True}],
                "params": {"prompt": "a", "model": "gpt-4"},
            },
            {
                "id": "b",
                "kind": "llm",
                "inputs": [{"name": "prompt", "kind": "text", "required": True}],
                "outputs": [{"name": "text", "kind": "text", "required": True}],
                "params": {"prompt": "b", "model": "gpt-4"},
            },
        ],
        "edges": [
            {"source": "a", "source_port": "text", "target": "b", "target_port": "prompt"},
            {"source": "b", "source_port": "text", "target": "a", "target_port": "prompt"},
        ],
    }


# ---------- 测试用桩 executor ----------


class StubExecutor(NodeExecutor):
    """返回固定输出的桩。"""

    def __init__(self, output_value: str = "result"):
        self.output_value = output_value
        self.executed: list[str] = []

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        self.executed.append(ctx.node.id)
        out = f"{self.output_value}:{ctx.node.id}"
        return {"text": out, "output": out}


class FailingExecutor(NodeExecutor):
    """总是失败的桩。"""

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        raise RuntimeError(f"boom:{ctx.node.id}")


class CollectingExecutor(NodeExecutor):
    """收集输入并返回。"""

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        return {"text": str(ctx.inputs), "output": str(ctx.inputs)}


# ======================================================================
# 端口定义与注册
# ======================================================================


class TestSubgraphPorts:
    """SUBGRAPH 节点的端口定义和注册。"""

    def test_subgraph_inputs(self) -> None:
        assert len(SUBGRAPH_INPUTS) == 1
        assert SUBGRAPH_INPUTS[0].name == "input"
        assert SUBGRAPH_INPUTS[0].kind == PortKind.ANY
        assert SUBGRAPH_INPUTS[0].required is False

    def test_subgraph_outputs(self) -> None:
        assert len(SUBGRAPH_OUTPUTS) == 1
        assert SUBGRAPH_OUTPUTS[0].name == "output"
        assert SUBGRAPH_OUTPUTS[0].kind == PortKind.ANY

    def test_required_params_contains_graph(self) -> None:
        assert "graph" in NODE_REQUIRED_PARAMS[NodeKind.SUBGRAPH]

    def test_node_kind_value(self) -> None:
        assert NodeKind.SUBGRAPH.value == "subgraph"

    def test_registered_in_port_dicts(self) -> None:
        assert NodeKind.SUBGRAPH in NODE_INPUT_PORTS
        assert NodeKind.SUBGRAPH in NODE_OUTPUT_PORTS
        assert NODE_INPUT_PORTS[NodeKind.SUBGRAPH] is SUBGRAPH_INPUTS
        assert NODE_OUTPUT_PORTS[NodeKind.SUBGRAPH] is SUBGRAPH_OUTPUTS


# ======================================================================
# 节点注册
# ======================================================================


class TestSubgraphNodeRegistry:
    """SUBGRAPH 在节点注册表中的注册。"""

    def test_available_kinds_contains_subgraph(self) -> None:
        assert "subgraph" in available_node_kinds()

    def test_factory_returns_subgraph_params(self) -> None:
        cls = NodeFactory("subgraph")
        assert cls.__name__ == "SubgraphNodeParams"

    def test_all_eight_kinds_registered(self) -> None:
        kinds = set(available_node_kinds())
        assert kinds == {
            "llm", "tool", "rag", "code", "branch", "loop", "eval", "subgraph",
        }


# ======================================================================
# 参数模型
# ======================================================================


class TestSubgraphParams:
    """SubgraphNodeParams 参数模型。"""

    def test_basic_construction(self) -> None:
        params = SubgraphNodeParams(graph={"version": "1", "nodes": [], "edges": []})
        assert params.graph["version"] == "1"

    def test_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        params = SubgraphNodeParams(graph={"version": "1", "nodes": [], "edges": []})
        with pytest.raises(FrozenInstanceError):
            params.graph = {}  # type: ignore[misc]


# ======================================================================
# 执行器
# ======================================================================


class TestSubgraphNodeExecutor:
    """SubgraphNodeExecutor 执行逻辑。"""

    @pytest.mark.asyncio
    async def test_executes_simple_subgraph(self) -> None:
        """子图含 1 个 LLM 节点，返回其 output。"""
        node = _subgraph_node(graph=_simple_subgraph())
        node_executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.LLM: StubExecutor("hello"),
        }
        executor = SubgraphNodeExecutor(node_executors)
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        # 单终端节点 + output key → 直接返回该值
        assert result["output"] == "hello:inner_llm"

    @pytest.mark.asyncio
    async def test_missing_graph_raises(self) -> None:
        """缺少 graph 参数 → ValueError。"""
        node = NodeBase(
            id="sub_0",
            kind=NodeKind.SUBGRAPH,
            inputs=SUBGRAPH_INPUTS,
            outputs=SUBGRAPH_OUTPUTS,
            params={},
        )
        executor = SubgraphNodeExecutor({})
        ctx = NodeExecutionContext(node=node, inputs={})
        with pytest.raises(ValueError, match="graph"):
            await executor.execute(ctx)

    @pytest.mark.asyncio
    async def test_subgraph_failure_raises(self) -> None:
        """子图执行失败 → RuntimeError 含错误信息。"""
        node = _subgraph_node(graph=_simple_subgraph())
        node_executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.LLM: FailingExecutor(),
        }
        executor = SubgraphNodeExecutor(node_executors)
        ctx = NodeExecutionContext(node=node, inputs={})
        with pytest.raises(RuntimeError, match="子图执行失败"):
            await executor.execute(ctx)

    @pytest.mark.asyncio
    async def test_external_input_passed_to_subgraph(self) -> None:
        """外部输入透传到子图源节点。"""
        # 子图的 LLM 节点用 CollectingExecutor 收集输入
        node = _subgraph_node(graph=_simple_subgraph())
        node_executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.LLM: CollectingExecutor(),
        }
        executor = SubgraphNodeExecutor(node_executors)
        ctx = NodeExecutionContext(
            node=node, inputs={"input": "external_data"}
        )
        result = await executor.execute(ctx)
        # 子图源节点的 inputs 中应含 "input": "external_data"
        assert "external_data" in result["output"]

    @pytest.mark.asyncio
    async def test_empty_subgraph_returns_none(self) -> None:
        """空子图（无节点）→ 返回 None。"""
        node = _subgraph_node(graph={"version": "1", "nodes": [], "edges": []})
        executor = SubgraphNodeExecutor({})
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        assert result["output"] is None

    @pytest.mark.asyncio
    async def test_two_node_subgraph(self) -> None:
        """两节点子图（LLM → CODE）正确执行，返回终端节点输出。"""
        node = _subgraph_node(graph=_two_node_subgraph())
        node_executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.LLM: StubExecutor("llm_out"),
            NodeKind.CODE: StubExecutor("code_out"),
        }
        executor = SubgraphNodeExecutor(node_executors)
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        # 终端节点是 n2（CODE），其 output 字段
        assert "code_out:n2" in result["output"]

    @pytest.mark.asyncio
    async def test_nested_subgraph(self) -> None:
        """子图内部再含 SUBGRAPH 节点（2 层嵌套）正常执行。"""
        inner_subgraph = _simple_subgraph("inner")
        outer_subgraph = {
            "version": "1",
            "nodes": [
                {
                    "id": "outer_sub",
                    "kind": "subgraph",
                    "inputs": [{"name": "input", "kind": "any", "required": False}],
                    "outputs": [{"name": "output", "kind": "any", "required": True}],
                    "params": {"graph": inner_subgraph},
                }
            ],
            "edges": [],
        }
        node = _subgraph_node(graph=outer_subgraph)
        node_executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.LLM: StubExecutor("deep"),
            NodeKind.SUBGRAPH: SubgraphNodeExecutor(
                {NodeKind.LLM: StubExecutor("deep")}
            ),
        }
        executor = SubgraphNodeExecutor(node_executors)
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        assert "deep:inner_llm" in result["output"]

    @pytest.mark.asyncio
    async def test_depth_limit_exceeded(self) -> None:
        """递归深度超限 → RuntimeError。"""
        # 构造一个深度 > MAX_SUBGRAPH_DEPTH 的递归子图
        # 每层子图含一个 SUBGRAPH 节点，指向下一层
        def _recursive_subgraph(depth: int) -> dict[str, Any]:
            if depth == 0:
                return _simple_subgraph()
            return {
                "version": "1",
                "nodes": [
                    {
                        "id": f"sub_{depth}",
                        "kind": "subgraph",
                        "inputs": [{"name": "input", "kind": "any", "required": False}],
                        "outputs": [{"name": "output", "kind": "any", "required": True}],
                        "params": {"graph": _recursive_subgraph(depth - 1)},
                    }
                ],
                "edges": [],
            }

        node = _subgraph_node(graph=_recursive_subgraph(MAX_SUBGRAPH_DEPTH + 1))
        node_executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.LLM: StubExecutor("x"),
            NodeKind.SUBGRAPH: None,  # placeholder, replaced below
        }
        executor = SubgraphNodeExecutor(node_executors)
        # 填入自身引用以支持递归
        node_executors[NodeKind.SUBGRAPH] = executor
        ctx = NodeExecutionContext(node=node, inputs={})
        with pytest.raises((RuntimeError, ValueError), match="深度"):
            await executor.execute(ctx)

    @pytest.mark.asyncio
    async def test_workflow_graph_object_accepted(self) -> None:
        """params["graph"] 是 WorkflowGraph 对象时也能执行。"""
        inner_graph = WorkflowGraph(
            nodes=(_llm("inner_llm"),),
            edges=(),
        )
        node = NodeBase(
            id="sub_0",
            kind=NodeKind.SUBGRAPH,
            inputs=SUBGRAPH_INPUTS,
            outputs=SUBGRAPH_OUTPUTS,
            params={"graph": inner_graph},
        )
        node_executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.LLM: StubExecutor("wg"),
        }
        executor = SubgraphNodeExecutor(node_executors)
        ctx = NodeExecutionContext(node=node, inputs={})
        result = await executor.execute(ctx)
        assert "wg:inner_llm" in result["output"]


# ======================================================================
# 校验
# ======================================================================


class TestSubgraphValidation:
    """SUBGRAPH 节点的校验。"""

    def test_missing_graph_param_rejected(self) -> None:
        """缺少 graph 参数 → error。"""
        node = NodeBase(
            id="sub_0",
            kind=NodeKind.SUBGRAPH,
            inputs=SUBGRAPH_INPUTS,
            outputs=SUBGRAPH_OUTPUTS,
            params={},
        )
        graph = WorkflowGraph(nodes=(node,))
        report = validate_graph(graph)
        assert not report.ok
        assert any("graph" in e.message for e in report.errors)

    def test_valid_subgraph_ok(self) -> None:
        """有效子图 → 校验通过。"""
        node = _subgraph_node(graph=_simple_subgraph())
        graph = WorkflowGraph(nodes=(node,))
        report = validate_graph(graph)
        assert report.ok, [e.message for e in report.errors]

    def test_cyclic_subgraph_rejected(self) -> None:
        """子图内有环 → error（带 subgraph 前缀）。"""
        node = _subgraph_node(graph=_cyclic_subgraph())
        graph = WorkflowGraph(nodes=(node,))
        report = validate_graph(graph)
        assert not report.ok
        assert any("环" in e.message for e in report.errors)
        assert any("subgraph[sub_0]" in e.field for e in report.errors)

    def test_duplicate_node_id_in_subgraph_rejected(self) -> None:
        """子图内节点 id 重复 → error。"""
        sub = {
            "version": "1",
            "nodes": [
                {
                    "id": "dup",
                    "kind": "llm",
                    "inputs": [{"name": "prompt", "kind": "text", "required": True}],
                    "outputs": [{"name": "text", "kind": "text", "required": True}],
                    "params": {"prompt": "a", "model": "gpt-4"},
                },
                {
                    "id": "dup",
                    "kind": "llm",
                    "inputs": [{"name": "prompt", "kind": "text", "required": True}],
                    "outputs": [{"name": "text", "kind": "text", "required": True}],
                    "params": {"prompt": "b", "model": "gpt-4"},
                },
            ],
            "edges": [],
        }
        node = _subgraph_node(graph=sub)
        graph = WorkflowGraph(nodes=(node,))
        report = validate_graph(graph)
        assert not report.ok
        assert any("重复" in e.message for e in report.errors)

    def test_type_incompat_in_subgraph_rejected(self) -> None:
        """子图内端口类型不兼容 → error。"""
        sub = {
            "version": "1",
            "nodes": [
                {
                    "id": "rag_n",
                    "kind": "rag",
                    "inputs": [{"name": "query", "kind": "text", "required": True}],
                    "outputs": [{"name": "documents", "kind": "documents", "required": True}],
                    "params": {"query": "q"},
                },
                {
                    "id": "llm_n",
                    "kind": "llm",
                    "inputs": [{"name": "prompt", "kind": "text", "required": True}],
                    "outputs": [{"name": "text", "kind": "text", "required": True}],
                    "params": {"prompt": "p", "model": "gpt-4"},
                },
            ],
            "edges": [
                {
                    "source": "rag_n", "source_port": "documents",
                    "target": "llm_n", "target_port": "prompt",
                },
            ],
        }
        node = _subgraph_node(graph=sub)
        graph = WorkflowGraph(nodes=(node,))
        report = validate_graph(graph)
        assert not report.ok
        assert any("类型不兼容" in e.message for e in report.errors)

    def test_missing_required_params_in_subgraph_rejected(self) -> None:
        """子图内节点缺必填参数 → error。"""
        sub = {
            "version": "1",
            "nodes": [
                {
                    "id": "llm_n",
                    "kind": "llm",
                    "inputs": [{"name": "prompt", "kind": "text", "required": True}],
                    "outputs": [{"name": "text", "kind": "text", "required": True}],
                    "params": {},  # 缺 prompt 和 model
                },
            ],
            "edges": [],
        }
        node = _subgraph_node(graph=sub)
        graph = WorkflowGraph(nodes=(node,))
        report = validate_graph(graph)
        assert not report.ok
        assert any("缺少必填参数" in e.message for e in report.errors)

    def test_nested_subgraph_validation(self) -> None:
        """3 层嵌套子图校验：最内层有环 → error。"""
        inner_cyclic = _cyclic_subgraph()
        mid = {
            "version": "1",
            "nodes": [
                {
                    "id": "mid_sub",
                    "kind": "subgraph",
                    "inputs": [{"name": "input", "kind": "any", "required": False}],
                    "outputs": [{"name": "output", "kind": "any", "required": True}],
                    "params": {"graph": inner_cyclic},
                }
            ],
            "edges": [],
        }
        outer_node = _subgraph_node(graph=mid)
        graph = WorkflowGraph(nodes=(outer_node,))
        report = validate_graph(graph)
        assert not report.ok
        assert any("环" in e.message for e in report.errors)

    def test_depth_limit_in_validation(self) -> None:
        """校验时嵌套深度超限 → error。"""
        from ariadne.graph_module.validate import _MAX_SUBGRAPH_DEPTH

        def _recursive_subgraph(depth: int) -> dict[str, Any]:
            if depth == 0:
                return _simple_subgraph()
            return {
                "version": "1",
                "nodes": [
                    {
                        "id": f"sub_{depth}",
                        "kind": "subgraph",
                        "inputs": [{"name": "input", "kind": "any", "required": False}],
                        "outputs": [{"name": "output", "kind": "any", "required": True}],
                        "params": {"graph": _recursive_subgraph(depth - 1)},
                    }
                ],
                "edges": [],
            }

        node = _subgraph_node(graph=_recursive_subgraph(_MAX_SUBGRAPH_DEPTH + 1))
        graph = WorkflowGraph(nodes=(node,))
        report = validate_graph(graph)
        assert not report.ok
        assert any("深度" in e.message for e in report.errors)

    def test_subgraph_deserialize_failure_rejected(self) -> None:
        """子图反序列化失败 → error。"""
        node = _subgraph_node(graph={"version": "1", "nodes": "not_a_list", "edges": []})
        graph = WorkflowGraph(nodes=(node,))
        report = validate_graph(graph)
        # 反序列化可能报错或校验报错，都应 not ok
        # nodes 不是 list 时 _deserialize_graph 的 tuple(n for n in nodes_data) 会迭代字符串
        # 但如果 nodes_data 是 str，迭代会产生单字符 → 构造 NodeBase 会失败
        assert not report.ok

    def test_subgraph_with_valid_loop_ok(self) -> None:
        """子图含有效 Loop 节点 → 校验通过。"""
        loop_goal = {
            "task": "say hello",
            "assertions": [
                {"id": "a1", "kind": "regex", "spec": {"pattern": "hello"}, "blocking": True}
            ],
            "budget": {"max_iterations": 3, "max_cost_usd": 1.0},
            "mode": "retry",
        }
        sub = {
            "version": "1",
            "nodes": [
                {
                    "id": "loop_n",
                    "kind": "loop",
                    "inputs": [{"name": "input", "kind": "any", "required": False}],
                    "outputs": [
                        {"name": "output", "kind": "text", "required": True},
                        {"name": "iterations", "kind": "json", "required": True},
                        {"name": "converged", "kind": "json", "required": True},
                    ],
                    "params": {"goal": loop_goal},
                },
            ],
            "edges": [],
        }
        node = _subgraph_node(graph=sub)
        graph = WorkflowGraph(nodes=(node,))
        report = validate_graph(graph)
        assert report.ok, [e.message for e in report.errors]


# ======================================================================
# 序列化
# ======================================================================


class TestSubgraphSerialization:
    """SUBGRAPH 节点的序列化 round-trip。"""

    def test_round_trip_preserves_graph_param(self) -> None:
        """round-trip 保留 params["graph"] dict。"""
        node = _subgraph_node(graph=_simple_subgraph())
        graph = WorkflowGraph(nodes=(node,))
        spec = graph_to_spec(graph)
        result = spec_to_graph(spec)
        result_node = result.node_by_id("sub_0")
        assert result_node is not None
        assert result_node.kind == NodeKind.SUBGRAPH
        assert "graph" in result_node.params
        sub_graph = result_node.params["graph"]
        assert isinstance(sub_graph, WorkflowGraph)
        assert len(sub_graph.nodes) == 1
        assert sub_graph.nodes[0].id == "inner_llm"

    def test_round_trip_preserves_nested_nodes_and_edges(self) -> None:
        """round-trip 保留嵌套图的节点和边。"""
        node = _subgraph_node(graph=_two_node_subgraph())
        graph = WorkflowGraph(nodes=(node,))
        spec = graph_to_spec(graph)
        result = spec_to_graph(spec)
        result_node = result.node_by_id("sub_0")
        assert result_node is not None
        sub_graph: WorkflowGraph = result_node.params["graph"]
        assert len(sub_graph.nodes) == 2
        assert len(sub_graph.edges) == 1
        assert sub_graph.edges[0].source == "n1"
        assert sub_graph.edges[0].target == "n2"

    def test_yaml_round_trip(self) -> None:
        """YAML round-trip 保留 SUBGRAPH 节点。"""
        node = _subgraph_node(graph=_simple_subgraph())
        graph = WorkflowGraph(nodes=(node,))
        yaml_str = graph_to_spec_yaml(graph)
        result = spec_yaml_to_graph(yaml_str)
        result_node = result.node_by_id("sub_0")
        assert result_node is not None
        assert result_node.kind == NodeKind.SUBGRAPH
        assert isinstance(result_node.params["graph"], WorkflowGraph)

    def test_nested_subgraph_round_trip(self) -> None:
        """2 层嵌套 SUBGRAPH round-trip。"""
        inner_sub = _simple_subgraph("inner")
        outer_sub = {
            "version": "1",
            "nodes": [
                {
                    "id": "outer_sub",
                    "kind": "subgraph",
                    "inputs": [{"name": "input", "kind": "any", "required": False}],
                    "outputs": [{"name": "output", "kind": "any", "required": True}],
                    "params": {"graph": inner_sub},
                }
            ],
            "edges": [],
        }
        node = _subgraph_node(graph=outer_sub)
        graph = WorkflowGraph(nodes=(node,))
        spec = graph_to_spec(graph)
        result = spec_to_graph(spec)
        result_node = result.node_by_id("sub_0")
        assert result_node is not None
        outer_sub_graph: WorkflowGraph = result_node.params["graph"]
        assert len(outer_sub_graph.nodes) == 1
        inner_node = outer_sub_graph.nodes[0]
        assert inner_node.kind == NodeKind.SUBGRAPH
        assert isinstance(inner_node.params["graph"], WorkflowGraph)
        assert len(inner_node.params["graph"].nodes) == 1

    def test_empty_subgraph_round_trip(self) -> None:
        """空子图 round-trip。"""
        node = _subgraph_node(graph={"version": "1", "nodes": [], "edges": []})
        graph = WorkflowGraph(nodes=(node,))
        spec = graph_to_spec(graph)
        result = spec_to_graph(spec)
        result_node = result.node_by_id("sub_0")
        assert result_node is not None
        sub_graph: WorkflowGraph = result_node.params["graph"]
        assert sub_graph.is_empty

    def test_subgraph_with_loop_round_trip(self) -> None:
        """子图内含 Loop 节点 round-trip。"""
        loop_goal = {
            "task": "say hello",
            "assertions": [
                {"id": "a1", "kind": "regex", "spec": {"pattern": "hello"}, "blocking": True}
            ],
            "budget": {"max_iterations": 3, "max_cost_usd": 1.0},
            "mode": "retry",
        }
        sub = {
            "version": "1",
            "nodes": [
                {
                    "id": "loop_n",
                    "kind": "loop",
                    "inputs": [{"name": "input", "kind": "any", "required": False}],
                    "outputs": [
                        {"name": "output", "kind": "text", "required": True},
                        {"name": "iterations", "kind": "json", "required": True},
                        {"name": "converged", "kind": "json", "required": True},
                    ],
                    "params": {"goal": loop_goal},
                },
            ],
            "edges": [],
        }
        node = _subgraph_node(graph=sub)
        graph = WorkflowGraph(nodes=(node,))
        spec = graph_to_spec(graph)
        result = spec_to_graph(spec)
        result_node = result.node_by_id("sub_0")
        assert result_node is not None
        sub_graph: WorkflowGraph = result_node.params["graph"]
        assert len(sub_graph.nodes) == 1
        assert sub_graph.nodes[0].kind == NodeKind.LOOP
        assert sub_graph.nodes[0].params["goal"]["task"] == "say hello"

    def test_complex_graph_round_trip(self) -> None:
        """复杂图（LLM + SUBGRAPH + EVAL）round-trip。"""
        eval_node = NodeBase(
            id="eval_0",
            kind=NodeKind.EVAL,
            inputs=(Port(name="input", kind=PortKind.TEXT),),
            outputs=(
                Port(name="passed", kind=PortKind.JSON),
                Port(name="verdict", kind=PortKind.TEXT),
            ),
            params={"assertions": []},
        )
        sub_node = _subgraph_node(graph=_simple_subgraph())
        llm_node = _llm("llm_0")
        graph = WorkflowGraph(
            nodes=(llm_node, sub_node, eval_node),
            edges=(
                _edge("llm_0", "text", "sub_0", "input"),
                _edge("sub_0", "output", "eval_0", "input"),
            ),
        )
        spec = graph_to_spec(graph)
        result = spec_to_graph(spec)
        assert len(result.nodes) == 3
        assert {n.kind for n in result.nodes} == {
            NodeKind.LLM, NodeKind.SUBGRAPH, NodeKind.EVAL,
        }
        result_sub = result.node_by_id("sub_0")
        assert result_sub is not None
        assert isinstance(result_sub.params["graph"], WorkflowGraph)

    def test_idempotent_double_round_trip(self) -> None:
        """双重 round-trip 幂等。"""
        node = _subgraph_node(graph=_simple_subgraph())
        graph = WorkflowGraph(nodes=(node,))
        spec1 = graph_to_spec(graph)
        graph1 = spec_to_graph(spec1)
        spec2 = graph_to_spec(graph1)
        graph2 = spec_to_graph(spec2)
        assert len(graph2.nodes) == len(graph1.nodes)
        sub2 = graph2.node_by_id("sub_0")
        assert sub2 is not None
        sub_graph2: WorkflowGraph = sub2.params["graph"]
        assert len(sub_graph2.nodes) == 1
        assert sub_graph2.nodes[0].id == "inner_llm"


# ======================================================================
# 图执行集成
# ======================================================================


class TestSubgraphInGraphExecutor:
    """SUBGRAPH 节点在完整图中执行。"""

    @pytest.mark.asyncio
    async def test_subgraph_in_full_graph(self) -> None:
        """SUBGRAPH 节点在完整图中正常执行。"""
        sub_node = _subgraph_node(graph=_simple_subgraph())
        llm_node = _llm("outer_llm")
        graph = WorkflowGraph(
            nodes=(llm_node, sub_node),
            edges=(_edge("outer_llm", "text", "sub_0", "input"),),
        )
        node_executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.LLM: StubExecutor("outer"),
            NodeKind.SUBGRAPH: SubgraphNodeExecutor(
                {NodeKind.LLM: StubExecutor("inner")}
            ),
        }
        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "start"},
            node_executors=node_executors,
        )
        assert result.errors == []
        assert result.node_states["outer_llm"] == NodeState.COMPLETED
        assert result.node_states["sub_0"] == NodeState.COMPLETED
        # sub_0 是终端节点，输出在 result.outputs 中
        assert "sub_0" in result.outputs
        assert "inner:inner_llm" in result.outputs["sub_0"]["output"]

    @pytest.mark.asyncio
    async def test_data_flow_through_subgraph(self) -> None:
        """上游 → SUBGRAPH → 下游数据流。"""
        sub_node = _subgraph_node(graph=_simple_subgraph())
        upstream = _llm("upstream")
        downstream = _llm("downstream")
        graph = WorkflowGraph(
            nodes=(upstream, sub_node, downstream),
            edges=(
                _edge("upstream", "text", "sub_0", "input"),
                _edge("sub_0", "output", "downstream", "prompt"),
            ),
        )
        # 下游用 CollectingExecutor 收集输入
        node_executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.LLM: CollectingExecutor(),
            NodeKind.SUBGRAPH: SubgraphNodeExecutor(
                {NodeKind.LLM: CollectingExecutor()}
            ),
        }
        result = await GraphExecutor().run(
            graph,
            inputs={"prompt": "start"},
            node_executors=node_executors,
        )
        assert result.errors == []
        assert result.node_states["downstream"] == NodeState.COMPLETED

    @pytest.mark.asyncio
    async def test_subgraph_failure_cascades(self) -> None:
        """SUBGRAPH 失败 → 下游 skip。"""
        sub_node = _subgraph_node(graph=_simple_subgraph())
        downstream = _llm("downstream")
        graph = WorkflowGraph(
            nodes=(sub_node, downstream),
            edges=(_edge("sub_0", "output", "downstream", "prompt"),),
        )
        node_executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.LLM: FailingExecutor(),
            NodeKind.SUBGRAPH: SubgraphNodeExecutor(
                {NodeKind.LLM: FailingExecutor()}
            ),
        }
        result = await GraphExecutor().run(
            graph,
            inputs={},
            node_executors=node_executors,
        )
        assert result.node_states["sub_0"] == NodeState.FAILED
        assert result.node_states["downstream"] == NodeState.SKIPPED

    @pytest.mark.asyncio
    async def test_subgraph_with_branch(self) -> None:
        """SUBGRAPH 与 Branch 节点组合。"""
        from ariadne.graph_module.executor import ROUTE_KEY
        from ariadne.graph_module.models import BRANCH_INPUTS, BRANCH_OUTPUTS

        branch_node = NodeBase(
            id="branch_0",
            kind=NodeKind.BRANCH,
            inputs=BRANCH_INPUTS,
            outputs=BRANCH_OUTPUTS,
            params={
                "condition": "yes",
                "branches": {"yes": "sub_0", "no": "other"},
                "__route_value": "yes",
            },
        )
        sub_node = _subgraph_node(graph=_simple_subgraph())
        other_node = _llm("other")
        graph = WorkflowGraph(
            nodes=(branch_node, sub_node, other_node),
            edges=(
                _edge("branch_0", "route", "sub_0", "input"),
                _edge("branch_0", "route", "other", "prompt"),
            ),
        )

        class _BranchExecutor(NodeExecutor):
            async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
                return {ROUTE_KEY: ctx.node.params.get("__route_value", "default")}

        node_executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.BRANCH: _BranchExecutor(),
            NodeKind.LLM: StubExecutor("lm"),
            NodeKind.SUBGRAPH: SubgraphNodeExecutor(
                {NodeKind.LLM: StubExecutor("inner")}
            ),
        }
        result = await GraphExecutor().run(
            graph,
            inputs={"input": "data"},
            node_executors=node_executors,
        )
        assert result.errors == []
        # 选中 yes → sub_0 执行，other 被 skip
        assert result.node_states["sub_0"] == NodeState.COMPLETED
        assert result.node_states["other"] == NodeState.SKIPPED

    @pytest.mark.asyncio
    async def test_concurrent_subgraphs(self) -> None:
        """两个 SUBGRAPH 同层并发执行。"""
        import time

        class _SlowExecutor(NodeExecutor):
            def __init__(self, marker: list[str], node_id: str):
                self.marker = marker
                self.node_id = node_id

            async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
                await asyncio.sleep(0.05)
                self.marker.append(self.node_id)
                return {"text": "done", "output": "done"}

        marker: list[str] = []
        sub1 = _subgraph_node("sub_1", graph=_simple_subgraph())
        sub2 = _subgraph_node("sub_2", graph=_simple_subgraph())
        graph = WorkflowGraph(nodes=(sub1, sub2), edges=())

        slow_llm = _SlowExecutor(marker, "llm")
        node_executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.LLM: slow_llm,
            NodeKind.SUBGRAPH: SubgraphNodeExecutor({NodeKind.LLM: slow_llm}),
        }
        start = time.perf_counter()
        result = await GraphExecutor().run(
            graph,
            inputs={},
            node_executors=node_executors,
        )
        elapsed = time.perf_counter() - start
        assert result.errors == []
        assert result.node_states["sub_1"] == NodeState.COMPLETED
        assert result.node_states["sub_2"] == NodeState.COMPLETED
        # 两个子图各执行一个 LLM 节点，并发 → 总时间 < 串行时间
        assert elapsed < 0.09  # 串行约 0.1s，并发约 0.05s

    @pytest.mark.asyncio
    async def test_terminal_output_collected(self) -> None:
        """SUBGRAPH 终端输出被 _collect_terminal_outputs 正确收集。"""
        sub_node = _subgraph_node(graph=_simple_subgraph())
        graph = WorkflowGraph(nodes=(sub_node,), edges=())
        node_executors: dict[NodeKind, NodeExecutor] = {
            NodeKind.LLM: StubExecutor("final"),
            NodeKind.SUBGRAPH: SubgraphNodeExecutor(
                {NodeKind.LLM: StubExecutor("final")}
            ),
        }
        result = await GraphExecutor().run(
            graph,
            inputs={},
            node_executors=node_executors,
        )
        assert result.errors == []
        # sub_0 是终端节点（无出边）
        assert "sub_0" in result.outputs
        assert "output" in result.outputs["sub_0"]
