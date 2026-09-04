"""graph_module 序列化测试 —— WorkflowGraph ↔ spec.yaml 双向转换。"""

from __future__ import annotations

from typing import Any

import pytest

from ariadne.graph_module.models import (
    BRANCH_INPUTS,
    LLM_INPUTS,
    LLM_OUTPUTS,
    LOOP_INPUTS,
    LOOP_OUTPUTS,
    Edge,
    NodeBase,
    NodeKind,
    Port,
    PortKind,
    WorkflowGraph,
)
from ariadne.graph_module.serialize import (
    graph_to_spec,
    graph_to_spec_yaml,
    spec_to_graph,
    spec_yaml_to_graph,
)

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


def _loop_node(
    node_id: str = "loop_0",
    goal: dict[str, Any] | None = None,
) -> NodeBase:
    if goal is None:
        goal = {
            "task": "say hello",
            "assertions": [
                {"id": "a1", "kind": "regex", "spec": {"pattern": "hello"}, "blocking": True}
            ],
            "budget": {"max_iterations": 3},
        }
    return NodeBase(
        id=node_id,
        kind=NodeKind.LOOP,
        inputs=LOOP_INPUTS,
        outputs=LOOP_OUTPUTS,
        params={"goal": goal},
    )


def _branch(node_id: str, branches: dict[str, str], condition: str = "true") -> NodeBase:
    return NodeBase(
        id=node_id,
        kind=NodeKind.BRANCH,
        inputs=BRANCH_INPUTS,
        outputs=(),
        params={"condition": condition, "branches": branches},
    )


# ======================================================================
# 基本往返转换
# ======================================================================


class TestRoundTrip:
    """图往返转换语义等价。"""

    def test_empty_graph_round_trip(self):
        """空图往返转换。"""
        graph = WorkflowGraph()
        spec = graph_to_spec(graph)
        result = spec_to_graph(spec)
        assert result.is_empty
        assert len(result.nodes) == 0
        assert len(result.edges) == 0

    def test_single_llm_node_round_trip(self):
        """单 LLM 节点图往返。"""
        a = _llm("a")
        graph = WorkflowGraph(nodes=(a,))
        spec = graph_to_spec(graph)
        result = spec_to_graph(spec)
        assert len(result.nodes) == 1
        assert result.nodes[0].id == "a"
        assert result.nodes[0].kind == NodeKind.LLM
        assert result.nodes[0].params["model"] == "gpt-4"

    def test_linear_graph_round_trip(self):
        """线性 A→B 图往返。"""
        a = _llm("a")
        b = _llm("b")
        graph = WorkflowGraph(
            nodes=(a, b),
            edges=(_edge("a", "text", "b", "prompt"),),
        )
        spec = graph_to_spec(graph)
        result = spec_to_graph(spec)
        assert len(result.nodes) == 2
        assert len(result.edges) == 1
        assert result.edges[0].source == "a"
        assert result.edges[0].target == "b"
        assert result.edges[0].source_port == "text"
        assert result.edges[0].target_port == "prompt"

    def test_diamond_graph_round_trip(self):
        """菱形图 A→B, A→C, B→D, C→D 往返。"""
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
        spec = graph_to_spec(graph)
        result = spec_to_graph(spec)
        assert len(result.nodes) == 4
        assert len(result.edges) == 4
        assert {e.source for e in result.edges} == {"a", "b", "c"}

    def test_port_kinds_preserved(self):
        """端口类型保留。"""
        a = _llm("a")
        b = NodeBase(
            id="b",
            kind=NodeKind.RAG,
            inputs=(Port(name="query", kind=PortKind.TEXT),),
            outputs=(Port(name="documents", kind=PortKind.DOCUMENTS),),
            params={"query": "q", "top_k": 3},
        )
        graph = WorkflowGraph(
            nodes=(a, b),
            edges=(_edge("a", "text", "b", "query"),),
        )
        spec = graph_to_spec(graph)
        result = spec_to_graph(spec)
        assert result.nodes[1].inputs[0].kind == PortKind.TEXT
        assert result.nodes[1].outputs[0].kind == PortKind.DOCUMENTS

    def test_params_preserved(self):
        """节点参数保留。"""
        a = _llm("a", prompt="custom prompt", temperature=0.3)
        graph = WorkflowGraph(nodes=(a,))
        spec = graph_to_spec(graph)
        result = spec_to_graph(spec)
        assert result.nodes[0].params["prompt"] == "custom prompt"
        assert result.nodes[0].params["temperature"] == 0.3


# ======================================================================
# Loop 节点序列化
# ======================================================================


class TestLoopSerialization:
    """Loop 节点 ↔ Spec 映射。"""

    def test_single_loop_graph_serializes_with_goal(self):
        """单 Loop 图序列化时提取 goal 到顶层。"""
        loop = _loop_node("loop_0")
        graph = WorkflowGraph(nodes=(loop,))
        spec = graph_to_spec(graph)
        assert "goal" in spec
        assert spec["goal"]["task"] == "say hello"
        assert "graph" in spec

    def test_single_loop_graph_round_trip(self):
        """单 Loop 图往返转换。"""
        loop = _loop_node("loop_0")
        graph = WorkflowGraph(nodes=(loop,))
        spec = graph_to_spec(graph)
        result = spec_to_graph(spec)
        assert len(result.nodes) == 1
        assert result.nodes[0].kind == NodeKind.LOOP
        assert "goal" in result.nodes[0].params

    def test_multi_loop_rejected(self):
        """多 Loop 图序列化被拒。"""
        loop1 = _loop_node("loop_0")
        loop2 = _loop_node("loop_1")
        graph = WorkflowGraph(nodes=(loop1, loop2))
        with pytest.raises(ValueError, match="多 Loop"):
            graph_to_spec(graph)

    def test_single_goal_spec_to_loop_graph(self):
        """旧格式单 goal Spec → 单 Loop 节点图。"""
        spec_dict = {
            "version": "1",
            "goal": {
                "task": "generate text",
                "assertions": [
                    {"id": "a", "kind": "regex", "spec": {"pattern": "."}, "blocking": True}
                ],
            },
        }
        graph = spec_to_graph(spec_dict)
        assert len(graph.nodes) == 1
        assert graph.nodes[0].kind == NodeKind.LOOP
        assert graph.nodes[0].params["goal"]["task"] == "generate text"

    def test_single_goal_spec_with_rules_to_loop(self):
        """旧格式含 rules 的 Spec → Loop 节点带 rules 参数。"""
        spec_dict = {
            "version": "1",
            "goal": {
                "task": "do task",
                "assertions": [
                    {"id": "a", "kind": "regex", "spec": {"pattern": "."}, "blocking": True}
                ],
            },
            "rules": [
                {
                    "id": "r1",
                    "category": "input",
                    "hook": "pre_model",
                    "when": "true",
                    "action": "warn",
                }
            ],
        }
        graph = spec_to_graph(spec_dict)
        assert "rules" in graph.nodes[0].params
        assert len(graph.nodes[0].params["rules"]) == 1


# ======================================================================
# Branch 节点序列化
# ======================================================================


class TestBranchSerialization:
    """Branch 节点序列化。"""

    def test_branch_graph_round_trip(self):
        """条件分支图往返转换。"""
        br = _branch("br", {"path_a": "a", "path_b": "b"})
        a = _llm("a")
        b = _llm("b")
        graph = WorkflowGraph(
            nodes=(br, a, b),
            edges=(
                _edge("br", "text", "a", "prompt"),
                _edge("br", "text", "b", "prompt"),
            ),
        )
        spec = graph_to_spec(graph)
        result = spec_to_graph(spec)
        assert len(result.nodes) == 3
        br_result = result.node_by_id("br")
        assert br_result is not None
        assert "branches" in br_result.params
        assert "path_a" in br_result.params["branches"]


# ======================================================================
# YAML 序列化
# ======================================================================


class TestYamlSerialization:
    """YAML 字符串序列化。"""

    def test_yaml_round_trip(self):
        """YAML 字符串往返转换。"""
        a = _llm("a")
        b = _llm("b")
        graph = WorkflowGraph(
            nodes=(a, b),
            edges=(_edge("a", "text", "b", "prompt"),),
        )
        yaml_str = graph_to_spec_yaml(graph)
        assert isinstance(yaml_str, str)
        result = spec_yaml_to_graph(yaml_str)
        assert len(result.nodes) == 2
        assert result.edges[0].source == "a"

    def test_yaml_has_graph_field(self):
        """YAML 输出包含 graph 字段。"""
        graph = WorkflowGraph(nodes=(_llm("a"),))
        yaml_str = graph_to_spec_yaml(graph)
        assert "graph:" in yaml_str
        assert "nodes:" in yaml_str


# ======================================================================
# 无效输入
# ======================================================================


class TestInvalidInput:
    """无效输入被拒。"""

    def test_invalid_node_kind_rejected(self):
        """无效节点类型被拒。"""
        bad_spec = {
            "version": "1",
            "graph": {
                "version": "1",
                "nodes": [
                    {
                        "id": "x",
                        "kind": "nonexistent",
                        "inputs": [],
                        "outputs": [],
                        "params": {},
                    }
                ],
                "edges": [],
            },
        }
        with pytest.raises(ValueError):
            spec_to_graph(bad_spec)

    def test_invalid_port_kind_rejected(self):
        """无效端口类型被拒。"""
        bad_spec = {
            "version": "1",
            "graph": {
                "version": "1",
                "nodes": [
                    {
                        "id": "x",
                        "kind": "llm",
                        "inputs": [{"name": "p", "kind": "nonexistent", "required": True}],
                        "outputs": [],
                        "params": {"prompt": "x", "model": "m"},
                    }
                ],
                "edges": [],
            },
        }
        with pytest.raises(ValueError):
            spec_to_graph(bad_spec)

    def test_empty_yaml_rejected(self):
        """空 YAML 被拒。"""
        with pytest.raises(ValueError):
            spec_yaml_to_graph("")


# ======================================================================
# 复杂图往返
# ======================================================================


class TestComplexGraphRoundTrip:
    """复杂图往返转换。"""

    def test_mixed_node_types_round_trip(self):
        """混合节点类型图往返。"""
        llm_node = _llm("llm_0")
        rag_node = NodeBase(
            id="rag_0",
            kind=NodeKind.RAG,
            inputs=(Port(name="query", kind=PortKind.TEXT),),
            outputs=(Port(name="documents", kind=PortKind.DOCUMENTS),),
            params={"query": "search", "top_k": 5},
        )
        eval_node = NodeBase(
            id="eval_0",
            kind=NodeKind.EVAL,
            inputs=(Port(name="input", kind=PortKind.TEXT),),
            outputs=(
                Port(name="passed", kind=PortKind.JSON),
                Port(name="verdict", kind=PortKind.TEXT),
            ),
            params={
                "assertions": [
                    {"id": "a1", "kind": "regex", "spec": {"pattern": "ok"}, "blocking": True}
                ]
            },
        )
        graph = WorkflowGraph(
            nodes=(llm_node, rag_node, eval_node),
            edges=(
                _edge("llm_0", "text", "rag_0", "query"),
                _edge("rag_0", "documents", "eval_0", "input"),
            ),
        )
        spec = graph_to_spec(graph)
        result = spec_to_graph(spec)
        assert len(result.nodes) == 3
        kinds = {n.kind for n in result.nodes}
        assert kinds == {NodeKind.LLM, NodeKind.RAG, NodeKind.EVAL}
        assert len(result.edges) == 2

    def test_version_preserved(self):
        """图版本号保留。"""
        graph = WorkflowGraph(nodes=(_llm("a"),), version="2")
        spec = graph_to_spec(graph)
        result = spec_to_graph(spec)
        assert result.version == "2"

    def test_idempotent_round_trip(self):
        """两次往返转换结果等价。"""
        a = _llm("a")
        b = _llm("b")
        graph = WorkflowGraph(
            nodes=(a, b),
            edges=(_edge("a", "text", "b", "prompt"),),
        )
        spec1 = graph_to_spec(graph)
        graph1 = spec_to_graph(spec1)
        spec2 = graph_to_spec(graph1)
        graph2 = spec_to_graph(spec2)
        assert {n.id for n in graph2.nodes} == {"a", "b"}
        assert len(graph2.edges) == 1
        assert graph2.edges[0].source == "a"
