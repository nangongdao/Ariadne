"""WorkflowGraph ↔ spec.yaml 双向序列化。

设计决策（docs/M5 §5）：
- 单 Loop 图 ↔ Spec 直接映射（goal → Loop 节点）
- 多 Loop 图 ↔ Spec 不支持（多 Loop 需 M5 并行池，本次不实现）
- 图序列化为扩展格式 GraphSpec（独立 schema，不修改已有 Spec 模型）

GraphSpec 格式（YAML 可读）：
    version: "1"
    graph:
      version: "1"
      nodes:
        - id: "n1"
          kind: "llm"
          inputs: [{name: "prompt", kind: "text", required: true}]
          outputs: [{name: "text", kind: "text"}]
          params: {prompt: "hello", model: "gpt-4"}
      edges:
        - source: "n1"
          source_port: "text"
          target: "n2"
          target_port: "prompt"

单向 Spec → 图：单 goal Spec 转换为单 Loop 节点图（兼容旧 spec）。
"""

from __future__ import annotations

from typing import Any

from ariadne.graph_module.models import (
    Edge,
    NodeBase,
    NodeKind,
    Port,
    PortKind,
    WorkflowGraph,
)
from ariadne.spec_module.loader import load_spec_from_dict
from ariadne.spec_module.schema import Spec


def graph_to_spec(graph: WorkflowGraph) -> dict[str, Any]:
    """WorkflowGraph → spec.yaml 可消费的 dict。

    单 Loop 图：提取 Loop 节点的 goal 作为 Spec.goal，其他节点作为 graph 扩展。
    无 Loop 图：直接序列化为 GraphSpec（只有 graph 字段）。
    多 Loop 图：抛 ValueError（本次不支持）。
    """
    loop_nodes = [n for n in graph.nodes if n.kind == NodeKind.LOOP]

    if len(loop_nodes) > 1:
        raise ValueError(
            f"多 Loop 图暂不支持序列化（发现 {len(loop_nodes)} 个 Loop 节点）"
        )

    result: dict[str, Any] = {"version": graph.version}

    if len(loop_nodes) == 1:
        # 单 Loop 图：提取 goal 到顶层
        loop_node = loop_nodes[0]
        goal_data = loop_node.params.get("goal")
        if goal_data is not None:
            result["goal"] = _normalize_goal_dict(goal_data)
        # rules/sandbox 从 Loop 节点 params 提取（如果有）
        if "rules" in loop_node.params:
            result["rules"] = loop_node.params["rules"]
        if "sandbox" in loop_node.params:
            result["sandbox"] = loop_node.params["sandbox"]

    # 序列化图结构
    result["graph"] = _serialize_graph(graph)

    return result


def spec_to_graph(spec_dict: dict[str, Any]) -> WorkflowGraph:
    """spec.yaml dict → WorkflowGraph。

    含 graph 字段：直接反序列化为完整图。
    不含 graph 字段但有 goal：单 goal → 单 Loop 节点图（兼容旧 spec）。
    """
    if "graph" in spec_dict:
        return _deserialize_graph(spec_dict["graph"])

    # 旧格式：单 goal Spec → 单 Loop 节点图
    spec = load_spec_from_dict(spec_dict)
    return _single_loop_graph_from_spec(spec)


def graph_to_spec_yaml(graph: WorkflowGraph) -> str:
    """WorkflowGraph → YAML 字符串。"""
    import yaml

    return yaml.dump(
        graph_to_spec(graph),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )


def spec_yaml_to_graph(yaml_str: str) -> WorkflowGraph:
    """YAML 字符串 → WorkflowGraph。"""
    import yaml

    data = yaml.safe_load(yaml_str)
    if not isinstance(data, dict):
        raise ValueError("YAML 内容不是 dict")
    return spec_to_graph(data)


# ---------- 内部序列化函数 ----------


def _serialize_graph(graph: WorkflowGraph) -> dict[str, Any]:
    """WorkflowGraph → dict（graph 字段内容）。"""
    return {
        "version": graph.version,
        "nodes": [_serialize_node(n) for n in graph.nodes],
        "edges": [_serialize_edge(e) for e in graph.edges],
    }


def _serialize_node(node: NodeBase) -> dict[str, Any]:
    """NodeBase → dict。"""
    params = dict(node.params)
    # SUBGRAPH 节点的 graph 参数可能是 WorkflowGraph 对象，需递归序列化
    if node.kind == NodeKind.SUBGRAPH and "graph" in params:
        graph_val = params["graph"]
        if isinstance(graph_val, WorkflowGraph):
            params["graph"] = _serialize_graph(graph_val)
    return {
        "id": node.id,
        "kind": node.kind.value,
        "inputs": [_serialize_port(p) for p in node.inputs],
        "outputs": [_serialize_port(p) for p in node.outputs],
        "params": params,
    }


def _serialize_port(port: Port) -> dict[str, Any]:
    """Port → dict。"""
    return {
        "name": port.name,
        "kind": port.kind.value,
        "required": port.required,
    }


def _serialize_edge(edge: Edge) -> dict[str, Any]:
    """Edge → dict。"""
    return {
        "source": edge.source,
        "source_port": edge.source_port,
        "target": edge.target,
        "target_port": edge.target_port,
    }


def _deserialize_graph(graph_data: dict[str, Any]) -> WorkflowGraph:
    """dict → WorkflowGraph。"""
    nodes_data = graph_data.get("nodes", [])
    edges_data = graph_data.get("edges", [])
    version = graph_data.get("version", "1")

    nodes = tuple(_deserialize_node(n) for n in nodes_data)
    edges = tuple(_deserialize_edge(e) for e in edges_data)

    return WorkflowGraph(nodes=nodes, edges=edges, version=version)


def _deserialize_node(node_data: dict[str, Any]) -> NodeBase:
    """dict → NodeBase。"""
    inputs = tuple(
        _deserialize_port(p) for p in node_data.get("inputs", [])
    )
    outputs = tuple(
        _deserialize_port(p) for p in node_data.get("outputs", [])
    )
    params = dict(node_data.get("params", {}))
    kind = NodeKind(node_data["kind"])
    # SUBGRAPH 节点的 graph 参数是 dict 形式，递归反序列化为 WorkflowGraph
    if kind == NodeKind.SUBGRAPH and "graph" in params:
        graph_val = params["graph"]
        if isinstance(graph_val, dict):
            params["graph"] = _deserialize_graph(graph_val)
    return NodeBase(
        id=node_data["id"],
        kind=kind,
        inputs=inputs,
        outputs=outputs,
        params=params,
    )


def _deserialize_port(port_data: dict[str, Any]) -> Port:
    """dict → Port。"""
    return Port(
        name=port_data["name"],
        kind=PortKind(port_data.get("kind", "any")),
        required=port_data.get("required", True),
    )


def _deserialize_edge(edge_data: dict[str, Any]) -> Edge:
    """dict → Edge。"""
    return Edge(
        source=edge_data["source"],
        source_port=edge_data["source_port"],
        target=edge_data["target"],
        target_port=edge_data["target_port"],
    )


def _normalize_goal_dict(goal_data: Any) -> dict[str, Any]:
    """规范化 goal dict（可能是 dict 或 GoalSpec）。"""
    if isinstance(goal_data, dict):
        return goal_data
    # 如果是 Pydantic 模型（GoalSpec），转 dict
    if hasattr(goal_data, "model_dump"):
        return dict(goal_data.model_dump())
    return dict(goal_data)


def _single_loop_graph_from_spec(spec: Spec) -> WorkflowGraph:
    """单 goal Spec → 单 Loop 节点图。

    把 Spec.goal 转换为一个 Loop 节点，其他 Spec 字段（rules/sandbox）
    存入 Loop 节点 params。
    """
    goal_dict = spec.goal.model_dump()

    loop_params: dict[str, Any] = {"goal": goal_dict}
    if spec.rules:
        loop_params["rules"] = [r.model_dump() for r in spec.rules]
    if spec.sandbox:
        loop_params["sandbox"] = spec.sandbox.model_dump()

    loop_node = NodeBase(
        id="loop_0",
        kind=NodeKind.LOOP,
        inputs=(
            Port(name="input", kind=PortKind.ANY, required=False),
        ),
        outputs=(
            Port(name="output", kind=PortKind.TEXT),
            Port(name="iterations", kind=PortKind.JSON),
            Port(name="converged", kind=PortKind.JSON),
        ),
        params=loop_params,
    )

    return WorkflowGraph(nodes=(loop_node,), edges=(), version=spec.version)


__all__ = [
    "graph_to_spec",
    "graph_to_spec_yaml",
    "spec_to_graph",
    "spec_yaml_to_graph",
]
