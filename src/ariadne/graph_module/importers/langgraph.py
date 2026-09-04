"""LangGraph 图定义导入 —— 转换为内部 WorkflowGraph。

D2 决策结论：Ariadne 自研执行器，但兼容导入 LangGraph 图定义。
用户的 LangGraph 图不该被迫重写，因此提供 import_langgraph(graph)
转换为内部 WorkflowGraph。

转换边界（docs/M5 §4.3）：
- LangGraph 节点 → Ariadne 节点（名称保留，类型推断为 tool）
- LangGraph 简单边 → Ariadne 边（route 端口）
- LangGraph 条件边 → Ariadne branch 节点 + 条件路由
- LangGraph checkpointer 不导入（用 Ariadne 自己的检查点）
- 不支持的构造显式报错而非静默降级

鸭子类型：不硬依赖 langgraph 库。只要传入对象暴露相同属性
（nodes/edges/branches）即可工作。这样 LangGraph 未安装时导入器仍可加载，
只是无法转换真实图。
"""

from __future__ import annotations

from typing import Any

from ariadne.graph_module.models import (
    BRANCH_INPUTS,
    BRANCH_OUTPUTS,
    CODE_OUTPUTS,
    Edge,
    NodeBase,
    NodeKind,
    Port,
    PortKind,
    WorkflowGraph,
)


class GraphImportError(Exception):
    """图导入时的不可恢复错误。不支持的构造、结构损坏等。"""


__all__ = ["GraphImportError", "import_langgraph"]


# START/END 哨兵节点名（LangGraph 用 "__start__"/"__end__"）
LG_START = "__start__"
LG_END = "__end__"


def import_langgraph(graph: Any) -> WorkflowGraph:
    """把 LangGraph 图定义转换为内部 WorkflowGraph。

    接受：
    - StateGraph 实例（未编译）
    - CompiledStateGraph 实例（编译后，取 .builder）

    鸭子类型：不 import langgraph，只按属性名访问。

    Raises:
        GraphImportError: 遇到不支持的构造或结构损坏。
    """
    builder = _resolve_builder(graph)
    _check_unsupported(builder)

    nodes_data = _extract_nodes(builder)
    edges_data = _extract_edges(builder)
    branches_data = _extract_branches(builder)

    # 构建 branch 节点（每个条件边源产生一个 branch 节点）
    branch_nodes: list[NodeBase] = []
    branch_edges: list[Edge] = []
    branch_target_ids: set[str] = set()  # 被 branch 路由的目标节点

    for source_id, branch_specs in branches_data:
        # 一个源节点可能有多个条件边（多个 condition_name）。
        # LangGraph 允许，但我们合并为一个 branch 节点。
        # 如果有多个 condition，取第一个的 condition 函数名作为 branch 条件名。
        branch_node_id = f"_branch_{source_id}"
        merged_ends: dict[str, str] = {}

        for _cond_name, spec in branch_specs:
            ends = _get_branch_ends(spec)
            if ends is None:
                raise GraphImportError(
                    f"条件边来自 {source_id} 的映射为 None（运行时路由），"
                    "Ariadne 不支持运行时决定的路由——需提供 path_map"
                )
            merged_ends.update(ends)

        branch_nodes.append(
            NodeBase(
                id=branch_node_id,
                kind=NodeKind.BRANCH,
                inputs=BRANCH_INPUTS,
                outputs=BRANCH_OUTPUTS,
                params={
                    "condition": "default",  # 条件求值由原函数决定，这里用字面路由
                    "branches": merged_ends,
                },
            )
        )

        # source → branch_node 边
        if source_id != LG_START:
            branch_edges.append(
                Edge(
                    source=source_id,
                    source_port="result",  # tool 节点的输出端口
                    target=branch_node_id,
                    target_port="input",
                )
            )
        else:
            # START → branch：branch 是图的入口
            branch_edges.append(
                Edge(
                    source=branch_node_id,
                    source_port="route",
                    target=branch_node_id,
                    target_port="input",
                )
            )

        # branch_node → 各分支目标
        for _return_val, target_id in merged_ends.items():
            if target_id == LG_END:
                continue
            branch_target_ids.add(target_id)
            branch_edges.append(
                Edge(
                    source=branch_node_id,
                    source_port="route",
                    target=target_id,
                    target_port="input",
                )
            )

    # 过滤掉已被 branch 路由覆盖的简单边
    # （源节点如果通过条件边路由，其普通出边应该被替换）
    branch_sources = {src for src, _ in branches_data}
    filtered_simple_edges: list[Edge] = []
    for src_id, tgt_id in edges_data:
        if tgt_id == LG_END:
            continue  # 跳过 END 边
        if src_id == LG_START:
            continue  # START 是哨兵，入口节点在 Ariadne 里就是没有入边的那个
        # 如果源节点有条件边，跳过其简单边（已被 branch 节点替代）
        if src_id in branch_sources and tgt_id not in branch_target_ids:
            # 源节点同时有条件边和简单边——LangGraph 允许，
            # 但在 Ariadne 中 branch 节点已接管路由，简单边冗余
            continue
        if src_id in branch_sources:
            continue  # branch 源的所有简单边都由 branch 节点处理
        filtered_simple_edges.append(
            Edge(
                source=src_id,
                source_port="result",
                target=tgt_id,
                target_port="input",
            )
        )

    # START 入口边直接丢弃：Ariadne 不用哨兵节点，DAG 的入口节点就是没有入边
    # 的那个，无需任何边来标记。曾经这里为入口节点造过一条 source==target 的
    # 边（本意是"把入口节点自己当源"），结果是自环 —— 而环检测会拒掉整张图，
    # 于是任何真实 LangGraph 图（必然有 add_edge(START, 首节点)）都导不进来。
    # 当时四条 validates 用例都没加 __start__ 边，自环从未被断言到。
    all_edges = filtered_simple_edges + branch_edges

    # 组装节点列表
    all_nodes = list(nodes_data) + branch_nodes

    return WorkflowGraph(
        nodes=tuple(all_nodes),
        edges=tuple(all_edges),
        version="1",
    )


# ---------- 内部辅助函数 ----------


def _resolve_builder(graph: Any) -> Any:
    """获取图构建器。

    如果传入的是编译后的图（有 .builder 属性），取 builder。
    否则直接用传入对象。
    """
    builder = getattr(graph, "builder", graph)
    if not hasattr(builder, "nodes"):
        raise GraphImportError(
            f"对象 {type(graph).__name__} 没有 .nodes 属性，"
            "不是有效的 LangGraph 图定义"
        )
    return builder


def _check_unsupported(builder: Any) -> None:
    """检查不支持的构造，显式报错。"""
    # 检查 waiting_edges（多源 fan-in）
    waiting: Any = getattr(builder, "waiting_edges", set())
    if waiting:
        raise GraphImportError(
            f"图包含 {len(waiting)} 条多源 fan-in 边（waiting_edges），"
            "Ariadne 的 DAG 执行器暂不支持多源 join"
        )

    # 检查 checkpointer 配置（编译后的图）
    checkpointer = getattr(builder, "checkpointer", None)
    if checkpointer is not None:
        # checkpointer 存在但不导入——这是预期的，不报错
        pass


def _extract_nodes(builder: Any) -> list[NodeBase]:
    """提取节点定义。LangGraph 节点名保留为 Ariadne 节点 id。

    跳过 START/END 哨兵节点和隐藏节点（以 __ 开头）。
    LangGraph 节点没有显式类型信息（都是 callable），统一映射为 tool 节点。
    """
    nodes: list[NodeBase] = []
    raw_nodes = builder.nodes

    if not isinstance(raw_nodes, dict):
        raise GraphImportError(f"nodes 不是 dict: {type(raw_nodes)}")

    for name in raw_nodes:
        if name.startswith("__"):
            continue  # 跳过 __start__, __end__, __error_handler__ 等
        nodes.append(
            NodeBase(
                id=name,
                kind=NodeKind.TOOL,
                inputs=(Port(name="input", kind=PortKind.ANY, required=False),),
                outputs=CODE_OUTPUTS,  # result: text
                params={
                    "cmd": name,  # 用节点名作为 cmd 标识
                    "args": {},
                    "_imported_from": "langgraph",
                },
            )
        )
    return nodes


def _extract_edges(builder: Any) -> list[tuple[str, str]]:
    """提取简单边 (start, end) 对。

    LangGraph edges 是 set[(start, end)]。也处理 _all_edges 属性。
    """
    edges_attr: Any = getattr(builder, "_all_edges", None)
    if edges_attr is None:
        edges_attr = getattr(builder, "edges", set())

    result: list[tuple[str, str]] = []
    for pair in edges_attr:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise GraphImportError(f"边格式异常: {pair}")
        result.append((str(pair[0]), str(pair[1])))
    return result


def _extract_branches(builder: Any) -> list[tuple[str, list[tuple[str, Any]]]]:
    """提取条件边。

    LangGraph branches: defaultdict[source_node, dict[condition_name, BranchSpec]]
    返回 [(source_id, [(condition_name, BranchSpec), ...]), ...]
    """
    branches = getattr(builder, "branches", None)
    if branches is None:
        return []

    result: list[tuple[str, list[tuple[str, Any]]]] = []
    for source_id, cond_dict in branches.items():
        specs: list[tuple[str, Any]] = []
        for cond_name, spec in cond_dict.items():
            specs.append((str(cond_name), spec))
        result.append((str(source_id), specs))
    return result


def _get_branch_ends(spec: Any) -> dict[str, str] | None:
    """从 BranchSpec 提取 ends 映射。

    BranchSpec.ends: dict[return_value, target_node] | None
    None 表示运行时路由（不支持）。
    """
    ends = getattr(spec, "ends", None)
    if ends is None:
        return None
    if not isinstance(ends, dict):
        return None
    # 把 key 转为字符串（LangGraph 的 return_value 可能是任意 hashable）
    return {str(k): str(v) for k, v in ends.items()}


