"""Subgraph 节点 —— 嵌套子图执行（多 Agent 编排）。

D7 多 Agent 编排的核心：一个 SUBGRAPH 节点内部运行一个完整的 WorkflowGraph，
实现 Agent 协作编排。外部看是单入单出的普通节点；内部是独立子图，
复用父图的 node_executors 分发，支持任意深度嵌套。

参数模型：SubgraphNodeParams（graph — 嵌套子图的 dict 表示）。
执行器：SubgraphNodeExecutor，注入 node_executors 字典，
构造 GraphExecutor 递归执行子图，返回子图终端节点输出。

输入映射：SUBGRAPH 外部端口 "input" 的值被映射到子图所有源节点
（无入边的节点）的输入端口名，使外部数据能进入子图。

深度追踪：用 contextvars.ContextVar 追踪嵌套深度，async 安全，
不依赖 NodeExecutionContext.inputs（_collect_inputs 只提取声明的端口名）。
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Any

from ariadne.graph_module import register_node
from ariadne.graph_module.executor import (
    GraphExecutor,
    NodeExecutionContext,
    NodeExecutor,
)
from ariadne.graph_module.models import NodeKind, WorkflowGraph
from ariadne.graph_module.serialize import _deserialize_graph

MAX_SUBGRAPH_DEPTH = 10

# async 安全的嵌套深度追踪（不经过 NodeExecutionContext.inputs，
# 因为 _collect_inputs 只提取声明的端口名，元数据会被丢弃）
_subgraph_depth: contextvars.ContextVar[int] = contextvars.ContextVar(
    "_subgraph_depth", default=0
)


@dataclass(frozen=True)
@register_node("subgraph")
class SubgraphNodeParams:
    """Subgraph 节点参数。

    graph: 嵌套子图的 dict 表示（serialize.py 的 _serialize_graph 输出格式）。
    执行时通过 _deserialize_graph 反序列化为 WorkflowGraph。
    """

    graph: dict[str, Any]


class SubgraphNodeExecutor(NodeExecutor):
    """Subgraph 节点执行器。

    从 node.params["graph"] 反序列化子图，将外部输入映射到子图源节点，
    用注入的 node_executors 递归执行子图，返回终端节点输出。

    嵌套深度通过 contextvars 追踪，防止无限递归。
    """

    def __init__(
        self, node_executors: dict[NodeKind, NodeExecutor]
    ) -> None:
        self._node_executors = node_executors

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        graph_data = ctx.node.params.get("graph")
        if graph_data is None:
            raise ValueError("SUBGRAPH 节点缺少 'graph' 参数")

        # 深度追踪：防止子图无限递归嵌套
        current_depth = _subgraph_depth.get()
        if current_depth >= MAX_SUBGRAPH_DEPTH:
            raise RuntimeError(
                f"子图嵌套深度超过最大值 {MAX_SUBGRAPH_DEPTH}"
                f"（节点 {ctx.node.id}）"
            )

        # 反序列化子图
        if isinstance(graph_data, WorkflowGraph):
            subgraph = graph_data
        elif isinstance(graph_data, dict):
            subgraph = _deserialize_graph(graph_data)
        else:
            raise ValueError(
                f"SUBGRAPH 节点 {ctx.node.id} 的 graph 参数类型无效: "
                f"{type(graph_data).__name__}"
            )

        # 外部输入映射到子图源节点
        sub_inputs = _map_external_inputs(ctx.inputs, subgraph)

        # 递归执行子图（深度 +1，用 contextvar 在 async 上下文中安全传播）
        token = _subgraph_depth.set(current_depth + 1)
        try:
            executor = GraphExecutor()
            result = await executor.run(
                subgraph,
                inputs=sub_inputs,
                node_executors=self._node_executors,
            )
        finally:
            _subgraph_depth.reset(token)

        if result.errors:
            raise RuntimeError(
                f"子图执行失败（节点 {ctx.node.id}）"
                f": {'; '.join(result.errors)}"
            )

        return {"output": _collect_subgraph_output(result)}


def _map_external_inputs(
    external_inputs: dict[str, Any],
    subgraph: WorkflowGraph,
) -> dict[str, Any]:
    """将 SUBGRAPH 外部输入映射到子图源节点的输入端口名。

    SUBGRAPH 的外部端口 "input" 的值被复制到子图所有源节点
    （无入边的节点）的每个输入端口名，使外部数据能进入子图。
    """
    sub_inputs: dict[str, Any] = {}

    # 找子图源节点（无入边）及其输入端口名
    has_incoming: set[str] = set()
    for edge in subgraph.edges:
        has_incoming.add(edge.target)

    source_port_names: set[str] = set()
    for node in subgraph.nodes:
        if node.id not in has_incoming:
            for port in node.inputs:
                source_port_names.add(port.name)

    # 外部 "input" 端口的值映射到源节点的输入端口名
    external_value = external_inputs.get("input")
    if external_value is not None:
        for port_name in source_port_names:
            sub_inputs[port_name] = external_value

    # 其他匹配端口名的输入也透传
    for k, v in external_inputs.items():
        if k != "input" and k in source_port_names:
            sub_inputs[k] = v

    return sub_inputs


def _collect_subgraph_output(result: Any) -> Any:
    """从子图执行结果收集输出。

    单终端节点且含 "output" key → 直接返回该值（与 LOOP 节点对齐）。
    多终端节点 → 返回 {node_id: output_dict} 的合并 dict。
    无终端节点 → 返回 None。
    """
    outputs = result.outputs
    if not outputs:
        return None

    if len(outputs) == 1:
        single_output = next(iter(outputs.values()))
        if isinstance(single_output, dict) and "output" in single_output:
            return single_output["output"]
        return single_output

    # 多终端节点：返回全部终端输出
    return outputs


__all__ = [
    "MAX_SUBGRAPH_DEPTH",
    "SubgraphNodeExecutor",
    "SubgraphNodeParams",
]
