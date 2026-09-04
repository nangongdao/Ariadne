"""graph_module 模型测试 —— WorkflowGraph/Node/Edge/Port 数据结构。"""

from __future__ import annotations

import pytest

from ariadne.graph_module.models import (
    CODE_INPUTS,
    CODE_OUTPUTS,
    LLM_INPUTS,
    LLM_OUTPUTS,
    LOOP_INPUTS,
    LOOP_OUTPUTS,
    NODE_INPUT_PORTS,
    NODE_OUTPUT_PORTS,
    NODE_REQUIRED_PARAMS,
    Edge,
    NodeBase,
    NodeKind,
    Port,
    PortKind,
    WorkflowGraph,
    port_compatible,
)


class TestPortKind:
    def test_enum_values(self) -> None:
        assert PortKind.TEXT == "text"
        assert PortKind.DOCUMENTS == "documents"
        assert PortKind.JSON == "json"
        assert PortKind.ARTIFACT == "artifact"
        assert PortKind.ANY == "any"

    def test_from_string(self) -> None:
        assert PortKind("text") is PortKind.TEXT
        assert PortKind("any") is PortKind.ANY


class TestNodeKind:
    def test_all_eight_kinds(self) -> None:
        assert len(NodeKind) == 8
        assert {k.value for k in NodeKind} == {
            "llm", "tool", "rag", "code", "branch", "loop", "eval", "subgraph",
        }


class TestPort:
    def test_default_kind_is_any(self) -> None:
        p = Port(name="in")
        assert p.kind is PortKind.ANY
        assert p.required is True

    def test_custom_kind(self) -> None:
        p = Port(name="text_out", kind=PortKind.TEXT, required=False)
        assert p.kind is PortKind.TEXT
        assert p.required is False

    def test_frozen(self) -> None:
        p = Port(name="x")
        with pytest.raises(AttributeError):
            p.name = "y"  # type: ignore[misc]


class TestNodeBase:
    def test_construct_with_defaults(self) -> None:
        n = NodeBase(id="n1", kind=NodeKind.LLM)
        assert n.id == "n1"
        assert n.kind is NodeKind.LLM
        assert n.inputs == ()
        assert n.outputs == ()
        assert n.params == {}

    def test_with_ports(self) -> None:
        n = NodeBase(
            id="llm1",
            kind=NodeKind.LLM,
            inputs=LLM_INPUTS,
            outputs=LLM_OUTPUTS,
            params={"prompt": "hello", "model": "gpt-4"},
        )
        assert len(n.inputs) == 1
        assert n.inputs[0].name == "prompt"
        assert len(n.outputs) == 1
        assert n.outputs[0].name == "text"
        assert n.params["model"] == "gpt-4"

    def test_frozen(self) -> None:
        n = NodeBase(id="n1", kind=NodeKind.LLM)
        with pytest.raises(AttributeError):
            n.id = "n2"  # type: ignore[misc]

    def test_input_by_name(self) -> None:
        n = NodeBase(id="n1", kind=NodeKind.LLM, inputs=LLM_INPUTS)
        assert n.input_by_name("prompt") is not None
        assert n.input_by_name("prompt").name == "prompt"
        assert n.input_by_name("nonexistent") is None

    def test_output_by_name(self) -> None:
        n = NodeBase(id="n1", kind=NodeKind.LLM, outputs=LLM_OUTPUTS)
        assert n.output_by_name("text") is not None
        assert n.output_by_name("text").kind is PortKind.TEXT
        assert n.output_by_name("nonexistent") is None


class TestEdge:
    def test_construct(self) -> None:
        e = Edge(source="a", source_port="text", target="b", target_port="prompt")
        assert e.source == "a"
        assert e.source_port == "text"
        assert e.target == "b"
        assert e.target_port == "prompt"

    def test_frozen(self) -> None:
        e = Edge(source="a", source_port="out", target="b", target_port="in")
        with pytest.raises(AttributeError):
            e.source = "c"  # type: ignore[misc]


class TestWorkflowGraph:
    def test_empty_graph(self) -> None:
        g = WorkflowGraph()
        assert g.is_empty
        assert len(g.nodes) == 0
        assert len(g.edges) == 0
        assert g.version == "1"

    def test_with_nodes_and_edges(self) -> None:
        a = NodeBase(id="a", kind=NodeKind.LLM, inputs=LLM_INPUTS, outputs=LLM_OUTPUTS)
        b = NodeBase(id="b", kind=NodeKind.EVAL, params={"assertions": []})
        e = Edge(source="a", source_port="text", target="b", target_port="input")
        g = WorkflowGraph(nodes=(a, b), edges=(e,))
        assert not g.is_empty
        assert len(g.nodes) == 2
        assert len(g.edges) == 1

    def test_node_by_id(self) -> None:
        a = NodeBase(id="a", kind=NodeKind.LLM)
        g = WorkflowGraph(nodes=(a,))
        assert g.node_by_id("a") is a
        assert g.node_by_id("nonexistent") is None

    def test_node_ids(self) -> None:
        a = NodeBase(id="a", kind=NodeKind.LLM)
        b = NodeBase(id="b", kind=NodeKind.TOOL)
        g = WorkflowGraph(nodes=(a, b))
        assert g.node_ids == frozenset({"a", "b"})

    def test_frozen(self) -> None:
        g = WorkflowGraph()
        with pytest.raises(AttributeError):
            g.version = "2"  # type: ignore[misc]


class TestPortCompatible:
    def test_any_compatible_with_all(self) -> None:
        for kind in PortKind:
            assert port_compatible(PortKind.ANY, kind)

    def test_same_type_compatible(self) -> None:
        assert port_compatible(PortKind.TEXT, PortKind.TEXT)
        assert port_compatible(PortKind.JSON, PortKind.JSON)
        assert port_compatible(PortKind.DOCUMENTS, PortKind.DOCUMENTS)

    def test_text_to_documents_incompatible(self) -> None:
        assert not port_compatible(PortKind.TEXT, PortKind.DOCUMENTS)

    def test_text_to_json_incompatible(self) -> None:
        assert not port_compatible(PortKind.TEXT, PortKind.JSON)

    def test_text_to_any_compatible(self) -> None:
        assert port_compatible(PortKind.TEXT, PortKind.ANY)

    def test_json_to_text_incompatible(self) -> None:
        assert not port_compatible(PortKind.JSON, PortKind.TEXT)


class TestStandardPorts:
    def test_llm_ports(self) -> None:
        assert len(LLM_INPUTS) == 1
        assert LLM_INPUTS[0].name == "prompt"
        assert LLM_INPUTS[0].kind is PortKind.TEXT
        assert len(LLM_OUTPUTS) == 1
        assert LLM_OUTPUTS[0].name == "text"

    def test_loop_ports(self) -> None:
        assert len(LOOP_INPUTS) == 1
        assert LOOP_INPUTS[0].required is False  # input 可选
        assert len(LOOP_OUTPUTS) == 3
        assert {p.name for p in LOOP_OUTPUTS} == {"output", "iterations", "converged"}

    def test_code_ports(self) -> None:
        assert len(CODE_INPUTS) == 1
        assert CODE_INPUTS[0].required is False
        assert len(CODE_OUTPUTS) == 1
        assert CODE_OUTPUTS[0].name == "result"

    def test_all_kinds_have_port_definitions(self) -> None:
        for kind in NodeKind:
            assert kind in NODE_INPUT_PORTS
            assert kind in NODE_OUTPUT_PORTS

    def test_all_kinds_have_required_params(self) -> None:
        for kind in NodeKind:
            assert kind in NODE_REQUIRED_PARAMS

    def test_branch_has_route_output_port(self) -> None:
        """Branch 节点有 route 输出端口用于连接到各分支目标节点。"""
        ports = NODE_OUTPUT_PORTS[NodeKind.BRANCH]
        assert len(ports) == 1
        assert ports[0].name == "route"
        assert ports[0].kind == PortKind.ANY
