"""LangGraph 导入器测试 —— 用桩对象模拟 LangGraph 图结构。

不依赖 langgraph 库——用 dataclass 桩模拟 StateGraph 的内部结构
（nodes/edges/branches/waiting_edges），验证 import_langgraph 的转换逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from ariadne.graph_module.importers.langgraph import (
    GraphImportError,
    import_langgraph,
)
from ariadne.graph_module.models import NodeKind, WorkflowGraph
from ariadne.graph_module.validate import validate_graph

# ---------- LangGraph 桩结构 ----------


@dataclass
class BranchStub:
    """模拟 langgraph.graph._branch.BranchSpec。"""

    ends: dict[str, str] | None = None
    path: Any = None  # 条件函数（导入器不调用）


@dataclass
class StateNodeStub:
    """模拟 langgraph.graph._node.StateNodeSpec。"""

    runnable: Any = None
    metadata: dict[str, Any] | None = None


@dataclass
class StateGraphStub:
    """模拟 langgraph.graph.state.StateGraph 的内部结构。

    鸭子类型：import_langgraph 只读 .nodes/.edges/.branches/.waiting_edges。
    """

    nodes: dict[str, StateNodeStub] = field(default_factory=dict)
    edges: set[tuple[str, str]] = field(default_factory=set)
    branches: dict[str, dict[str, BranchStub]] = field(default_factory=dict)
    waiting_edges: set[tuple[tuple[str, ...], str]] = field(default_factory=set)
    checkpointer: Any = None

    def add_node(self, name: str) -> None:
        self.nodes[name] = StateNodeStub()

    def add_edge(self, start: str, end: str) -> None:
        self.edges.add((start, end))

    def add_conditional_edges(
        self,
        source: str,
        ends: dict[str, str],
        cond_name: str = "condition",
    ) -> None:
        if source not in self.branches:
            self.branches[source] = {}
        self.branches[source][cond_name] = BranchStub(ends=ends)

    def add_waiting_edge(self, starts: tuple[str, ...], end: str) -> None:
        self.waiting_edges.add((starts, end))


@dataclass
class CompiledGraphStub:
    """模拟编译后的图（CompiledStateGraph）。"""

    builder: StateGraphStub = field(default_factory=StateGraphStub)
    checkpointer: Any = None


# ---------- 辅助函数 ----------


def _simple_graph() -> StateGraphStub:
    """A → B → C 的简单线性图。"""
    g = StateGraphStub()
    g.add_node("a")
    g.add_node("b")
    g.add_node("c")
    g.add_edge("a", "b")
    g.add_edge("b", "c")
    return g


# ---------- 基本转换测试 ----------


class TestImportBasic:
    def test_linear_graph_imports(self) -> None:
        """线性图 A → B → C 正确导入。"""
        g = _simple_graph()
        result = import_langgraph(g)

        assert isinstance(result, WorkflowGraph)
        node_ids = {n.id for n in result.nodes}
        assert node_ids == {"a", "b", "c"}

    def test_node_ids_preserved(self) -> None:
        """LangGraph 节点名保留为 Ariadne 节点 id。"""
        g = StateGraphStub()
        g.add_node("retrieve")
        g.add_node("generate")
        g.add_edge("retrieve", "generate")

        result = import_langgraph(g)
        ids = [n.id for n in result.nodes]
        assert "retrieve" in ids
        assert "generate" in ids

    def test_nodes_mapped_to_tool_kind(self) -> None:
        """LangGraph 节点无类型信息，统一映射为 tool 节点。"""
        g = _simple_graph()
        result = import_langgraph(g)

        for node in result.nodes:
            assert node.kind == NodeKind.TOOL

    def test_empty_graph(self) -> None:
        """空图导入返回空 WorkflowGraph。"""
        g = StateGraphStub()
        result = import_langgraph(g)
        assert result.is_empty
        assert len(result.edges) == 0

    def test_single_node(self) -> None:
        """单节点图导入。"""
        g = StateGraphStub()
        g.add_node("solo")
        result = import_langgraph(g)
        assert len(result.nodes) == 1
        assert result.nodes[0].id == "solo"

    def test_imported_from_metadata(self) -> None:
        """导入的节点 params 标记来源。"""
        g = _simple_graph()
        result = import_langgraph(g)
        for node in result.nodes:
            assert node.params.get("_imported_from") == "langgraph"


# ---------- 边转换测试 ----------


class TestImportEdges:
    def test_simple_edges_preserved(self) -> None:
        """简单边 A → B → C 保留。"""
        g = _simple_graph()
        result = import_langgraph(g)

        # 应有 a→b 和 b→c 两条边
        edge_pairs = {(e.source, e.target) for e in result.edges}
        assert ("a", "b") in edge_pairs
        assert ("b", "c") in edge_pairs

    def test_start_end_sentinels_skipped(self) -> None:
        """__start__ 和 __end__ 哨兵节点和边被跳过。"""
        g = StateGraphStub()
        g.add_node("a")
        g.add_edge("__start__", "a")
        g.add_edge("a", "__end__")

        result = import_langgraph(g)
        node_ids = {n.id for n in result.nodes}
        assert "__start__" not in node_ids
        assert "__end__" not in node_ids

    def test_hidden_nodes_skipped(self) -> None:
        """以 __ 开头的隐藏节点被跳过。"""
        g = StateGraphStub()
        g.add_node("visible")
        g.add_node("__hidden__")
        result = import_langgraph(g)
        node_ids = {n.id for n in result.nodes}
        assert "visible" in node_ids
        assert "__hidden__" not in node_ids

    def test_parallel_graph(self) -> None:
        """并行图 A → B, A → C 正确导入。"""
        g = StateGraphStub()
        g.add_node("a")
        g.add_node("b")
        g.add_node("c")
        g.add_edge("a", "b")
        g.add_edge("a", "c")

        result = import_langgraph(g)
        edge_pairs = {(e.source, e.target) for e in result.edges}
        assert ("a", "b") in edge_pairs
        assert ("a", "c") in edge_pairs

    def test_diamond_graph(self) -> None:
        """菱形图 A → B → D, A → C → D 正确导入。"""
        g = StateGraphStub()
        g.add_node("a")
        g.add_node("b")
        g.add_node("c")
        g.add_node("d")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", "d")
        g.add_edge("c", "d")

        result = import_langgraph(g)
        assert len(result.nodes) == 4
        edge_pairs = {(e.source, e.target) for e in result.edges}
        assert ("a", "b") in edge_pairs
        assert ("a", "c") in edge_pairs
        assert ("b", "d") in edge_pairs
        assert ("c", "d") in edge_pairs


# ---------- 条件边转换测试 ----------


class TestImportBranches:
    def test_conditional_edges_create_branch_node(self) -> None:
        """条件边创建 branch 节点。"""
        g = StateGraphStub()
        g.add_node("router")
        g.add_node("path_a")
        g.add_node("path_b")
        g.add_conditional_edges("router", {"a": "path_a", "b": "path_b"})

        result = import_langgraph(g)

        # 应有 _branch_router 节点
        branch_nodes = [n for n in result.nodes if n.kind == NodeKind.BRANCH]
        assert len(branch_nodes) == 1
        assert branch_nodes[0].id == "_branch_router"

    def test_branch_node_has_correct_params(self) -> None:
        """branch 节点 params 包含 branches 映射。"""
        g = StateGraphStub()
        g.add_node("router")
        g.add_node("path_a")
        g.add_node("path_b")
        g.add_conditional_edges("router", {"a": "path_a", "b": "path_b"})

        result = import_langgraph(g)
        branch = next(n for n in result.nodes if n.kind == NodeKind.BRANCH)
        assert branch.params["branches"] == {"a": "path_a", "b": "path_b"}

    def test_branch_edges_connect_to_targets(self) -> None:
        """branch 节点有到各分支目标的边。"""
        g = StateGraphStub()
        g.add_node("router")
        g.add_node("path_a")
        g.add_node("path_b")
        g.add_conditional_edges("router", {"a": "path_a", "b": "path_b"})

        result = import_langgraph(g)
        edge_pairs = {(e.source, e.target) for e in result.edges}
        assert ("_branch_router", "path_a") in edge_pairs
        assert ("_branch_router", "path_b") in edge_pairs

    def test_branch_replaces_simple_edges_from_source(self) -> None:
        """源节点的条件边替代其简单边。"""
        g = StateGraphStub()
        g.add_node("router")
        g.add_node("path_a")
        g.add_node("path_b")
        g.add_edge("router", "path_a")  # 简单边
        g.add_conditional_edges("router", {"a": "path_a", "b": "path_b"})

        result = import_langgraph(g)

        # router 不应直接连到 path_a（由 branch 节点连接）
        edge_pairs = {(e.source, e.target) for e in result.edges}
        assert ("router", "path_a") not in edge_pairs
        assert ("_branch_router", "path_a") in edge_pairs

    def test_multiple_conditions_merged(self) -> None:
        """同一源的多个条件边合并为一个 branch 节点。"""
        g = StateGraphStub()
        g.add_node("src")
        g.add_node("t1")
        g.add_node("t2")
        g.add_node("t3")
        g.add_conditional_edges("src", {"x": "t1"}, cond_name="cond1")
        g.add_conditional_edges("src", {"y": "t2", "z": "t3"}, cond_name="cond2")

        result = import_langgraph(g)
        branch_nodes = [n for n in result.nodes if n.kind == NodeKind.BRANCH]
        assert len(branch_nodes) == 1
        branches = branch_nodes[0].params["branches"]
        assert branches == {"x": "t1", "y": "t2", "z": "t3"}

    def test_branch_with_end_target(self) -> None:
        """条件边目标包含 __end__ 时不创建到 __end__ 的边。"""
        g = StateGraphStub()
        g.add_node("router")
        g.add_node("path_a")
        g.add_conditional_edges("router", {"a": "path_a", "end": "__end__"})

        result = import_langgraph(g)
        node_ids = {n.id for n in result.nodes}
        assert "__end__" not in node_ids
        edge_pairs = {(e.source, e.target) for e in result.edges}
        assert ("_branch_router", "path_a") in edge_pairs
        assert ("_branch_router", "__end__") not in edge_pairs


# ---------- 错误处理测试 ----------


class TestImportErrors:
    def test_runtime_routing_rejected(self) -> None:
        """ends=None（运行时路由）被明确拒绝。"""
        g = StateGraphStub()
        g.add_node("src")
        g.add_node("t1")
        g.add_conditional_edges("src", {})
        # 手动设置 ends 为 None
        g.branches["src"]["condition"].ends = None

        with pytest.raises(GraphImportError, match="运行时路由"):
            import_langgraph(g)

    def test_waiting_edges_rejected(self) -> None:
        """多源 fan-in 边（waiting_edges）被拒绝。"""
        g = StateGraphStub()
        g.add_node("a")
        g.add_node("b")
        g.add_node("c")
        g.add_waiting_edge(("a", "b"), "c")

        with pytest.raises(GraphImportError, match="fan-in"):
            import_langgraph(g)

    def test_invalid_object_rejected(self) -> None:
        """没有 .nodes 属性的对象被拒绝。"""
        with pytest.raises(GraphImportError, match="不是有效的 LangGraph"):
            import_langgraph("not a graph")

    def test_invalid_edge_format_rejected(self) -> None:
        """边格式异常被拒绝。"""
        g = StateGraphStub()
        g.add_node("a")
        g.edges.add(("a",))  # type: ignore[arg-type]  # 单元素 tuple

        with pytest.raises(GraphImportError, match="边格式异常"):
            import_langgraph(g)


# ---------- 编译后图测试 ----------


class TestImportCompiled:
    def test_compiled_graph_uses_builder(self) -> None:
        """编译后的图通过 .builder 获取原始结构。"""
        builder = _simple_graph()
        compiled = CompiledGraphStub(builder=builder)

        result = import_langgraph(compiled)
        node_ids = {n.id for n in result.nodes}
        assert node_ids == {"a", "b", "c"}


# ---------- 拓扑等价性验证 ----------


class TestTopologicalEquivalence:
    def test_linear_graph_validates(self) -> None:
        """导入后的线性图通过 validate_graph。"""
        g = _simple_graph()
        result = import_langgraph(g)
        report = validate_graph(result)
        assert report.ok, f"校验失败: {[e.message for e in report.errors]}"

    def test_diamond_graph_validates(self) -> None:
        """导入后的菱形图通过 validate_graph。"""
        g = StateGraphStub()
        g.add_node("a")
        g.add_node("b")
        g.add_node("c")
        g.add_node("d")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", "d")
        g.add_edge("c", "d")

        result = import_langgraph(g)
        report = validate_graph(result)
        assert report.ok, f"校验失败: {[e.message for e in report.errors]}"

    def test_branch_graph_validates(self) -> None:
        """含条件边的图导入后通过 validate_graph。"""
        g = StateGraphStub()
        g.add_node("router")
        g.add_node("path_a")
        g.add_node("path_b")
        g.add_conditional_edges("router", {"a": "path_a", "b": "path_b"})

        result = import_langgraph(g)
        report = validate_graph(result)
        assert report.ok, f"校验失败: {[e.message for e in report.errors]}"

    def test_complex_graph_validates(self) -> None:
        """复杂图（条件边 + 简单边混合）导入后通过 validate_graph。"""
        g = StateGraphStub()
        g.add_node("entry")
        g.add_node("router")
        g.add_node("path_a")
        g.add_node("path_b")
        g.add_node("finalize")
        g.add_edge("entry", "router")
        g.add_conditional_edges("router", {"a": "path_a", "b": "path_b"})
        g.add_edge("path_a", "finalize")
        g.add_edge("path_b", "finalize")

        result = import_langgraph(g)
        report = validate_graph(result)
        assert report.ok, f"校验失败: {[e.message for e in report.errors]}"

    def test_real_shape_with_start_edge_validates(self) -> None:
        """带 __start__ / __end__ 哨兵边的图（真实 LangGraph 的必然形态）可校验。

        这条补的是上面四条用例的共同盲区：它们都不加哨兵边，于是入口边处理
        那段代码从未被执行到。而 LangGraph 里 add_edge(START, 首节点) 是必写的，
        真实图 100% 命中这条路径。曾经该路径为入口节点造出 source==target 的
        自环，环检测直接拒图 —— 单元测试全绿，但没有一张真图能导进来。
        """
        g = StateGraphStub()
        g.add_node("a")
        g.add_node("b")
        g.add_edge("__start__", "a")
        g.add_edge("a", "b")
        g.add_edge("b", "__end__")

        result = import_langgraph(g)

        selfloops = [(e.source, e.target) for e in result.edges if e.source == e.target]
        assert not selfloops, f"入口边不该产生自环: {selfloops}"

        report = validate_graph(result)
        assert report.ok, f"校验失败: {[e.message for e in report.errors]}"

        # 入口节点没有入边，这才是 Ariadne 表达"入口"的方式
        assert not [e for e in result.edges if e.target == "a"]
        assert {(e.source, e.target) for e in result.edges} == {("a", "b")}

    def test_start_into_branch_source_validates(self) -> None:
        """__start__ 直连条件边源节点时也不能产生自环。"""
        g = StateGraphStub()
        g.add_node("router")
        g.add_node("path_a")
        g.add_node("path_b")
        g.add_edge("__start__", "router")
        g.add_conditional_edges("router", {"a": "path_a", "b": "path_b"})

        result = import_langgraph(g)

        selfloops = [(e.source, e.target) for e in result.edges if e.source == e.target]
        assert not selfloops, f"入口边不该产生自环: {selfloops}"

        report = validate_graph(result)
        assert report.ok, f"校验失败: {[e.message for e in report.errors]}"

    def test_topology_preserved_linear(self) -> None:
        """线性图导入后拓扑序保持。"""
        from graphlib import TopologicalSorter

        from ariadne.graph_module.executor import _build_predecessors

        g = _simple_graph()
        result = import_langgraph(g)

        ts: TopologicalSorter[str] = TopologicalSorter()
        preds = _build_predecessors(result)
        for node_id, p in preds.items():
            ts.add(node_id, *p)
        order = list(ts.static_order())

        # a 应在 b 前，b 应在 c 前
        assert order.index("a") < order.index("b")
        assert order.index("b") < order.index("c")
