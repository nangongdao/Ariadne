"""图设计时校验 —— 保存即校验（docs/M5 §4.2）。

不等运行时才报错。validate_graph 检查：
- 节点 id 唯一
- 边引用的节点/端口存在
- 环检测（Loop 节点外不允许成环）
- 类型兼容（上游输出 vs 下游输入）
- 必填参数缺失
- Loop 节点的断言可验证性（复用 M3 的 validate_goal）
- Subgraph 节点的嵌套子图递归校验

断言可验证性校验直接复用 M3 的规则，避免两处实现漂移。
子图校验递归调用 validate_graph，自动覆盖子图的全部检查。
"""

from __future__ import annotations

from graphlib import CycleError, TopologicalSorter

from ariadne.graph_module.models import (
    NODE_REQUIRED_PARAMS,
    NodeKind,
    WorkflowGraph,
    port_compatible,
)
from ariadne.loop_module.goal_validation import (
    ValidationIssue,
    ValidationReport,
    validate_goal,
)


def validate_graph(
    graph: WorkflowGraph,
    *,
    available_metrics: frozenset[str] = frozenset(),
    sandbox_available: bool = False,
    _subgraph_depth: int = 0,
) -> ValidationReport:
    """校验图的完整性，返回 ValidationReport。

    errors 阻止保存（API 返回 422），warnings 仅提示。
    _subgraph_depth 用于内部递归校验嵌套子图，外部不应传此参数。
    """
    issues: list[ValidationIssue] = []

    issues.extend(_check_node_ids(graph))
    issues.extend(_check_edges(graph))
    issues.extend(_check_cycle(graph))
    issues.extend(_check_type_compat(graph))
    issues.extend(_check_required_params(graph))
    issues.extend(_check_loop_assertions(graph, available_metrics, sandbox_available))
    issues.extend(_check_subgraph_validity(
        graph, available_metrics, sandbox_available, _subgraph_depth,
    ))

    return ValidationReport(issues=tuple(issues))


# ---------- 逐项校验 ----------


def _check_node_ids(graph: WorkflowGraph) -> list[ValidationIssue]:
    """节点 id 唯一性。"""
    seen: set[str] = set()
    issues: list[ValidationIssue] = []
    for node in graph.nodes:
        if node.id in seen:
            issues.append(
                ValidationIssue(
                    field=f"node:{node.id}",
                    message=f"重复的节点 id: {node.id}",
                )
            )
        seen.add(node.id)
    return issues


def _check_edges(graph: WorkflowGraph) -> list[ValidationIssue]:
    """边引用的节点和端口存在。"""
    issues: list[ValidationIssue] = []
    node_map = {n.id: n for n in graph.nodes}

    for edge in graph.edges:
        source = node_map.get(edge.source)
        if source is None:
            issues.append(
                ValidationIssue(
                    field=f"edge:{edge.source}->{edge.target}",
                    message=f"边引用了不存在的源节点: {edge.source}",
                )
            )
            continue
        target = node_map.get(edge.target)
        if target is None:
            issues.append(
                ValidationIssue(
                    field=f"edge:{edge.source}->{edge.target}",
                    message=f"边引用了不存在的目标节点: {edge.target}",
                )
            )
            continue

        if source.output_by_name(edge.source_port) is None:
            issues.append(
                ValidationIssue(
                    field=f"edge:{edge.source}.{edge.source_port}",
                    message=(
                        f"源节点 {edge.source} 没有输出端口: {edge.source_port}"
                    ),
                )
            )
        if target.input_by_name(edge.target_port) is None:
            issues.append(
                ValidationIssue(
                    field=f"edge:{edge.target}.{edge.target_port}",
                    message=(
                        f"目标节点 {edge.target} 没有输入端口: {edge.target_port}"
                    ),
                )
            )

    return issues


def _check_cycle(graph: WorkflowGraph) -> list[ValidationIssue]:
    """环检测：DAG 不允许成环（Loop 节点内部循环不算图上的环）。

    用 graphlib.TopologicalSorter 检测。CycleError → error。
    """
    if graph.is_empty:
        return []

    ts: TopologicalSorter[str] = TopologicalSorter()
    # 构建邻接表：每个节点的直接前驱
    predecessors: dict[str, set[str]] = {n.id: set() for n in graph.nodes}
    for edge in graph.edges:
        if edge.source in predecessors and edge.target in predecessors:
            predecessors[edge.target].add(edge.source)

    for node_id, preds in predecessors.items():
        ts.add(node_id, *preds)

    try:
        list(ts.static_order())
    except CycleError as exc:
        nodes_in_cycle = exc.args[1] if len(exc.args) > 1 else []
        cycle_desc = " -> ".join(str(n) for n in nodes_in_cycle) if nodes_in_cycle else "(unknown)"
        return [
            ValidationIssue(
                field="graph",
                message=f"检测到环: {cycle_desc}（Loop 节点外不允许成环）",
            )
        ]
    return []


def _check_type_compat(graph: WorkflowGraph) -> list[ValidationIssue]:
    """类型兼容：边的源端口类型与目标端口类型兼容。"""
    issues: list[ValidationIssue] = []
    node_map = {n.id: n for n in graph.nodes}

    for edge in graph.edges:
        source = node_map.get(edge.source)
        target = node_map.get(edge.target)
        if source is None or target is None:
            continue  # 悬空边已在 _check_edges 报告

        source_port = source.output_by_name(edge.source_port)
        target_port = target.input_by_name(edge.target_port)
        if source_port is None or target_port is None:
            continue  # 端口不存在已在 _check_edges 报告

        if not port_compatible(source_port.kind, target_port.kind):
            issues.append(
                ValidationIssue(
                    field=f"edge:{edge.source}.{edge.source_port}->{edge.target}.{edge.target_port}",
                    message=(
                        f"类型不兼容: {source_port.kind.value} → {target_port.kind.value}"
                        f"（{edge.source}.{edge.source_port} → {edge.target}.{edge.target_port}）"
                    ),
                )
            )

    return issues


def _check_required_params(graph: WorkflowGraph) -> list[ValidationIssue]:
    """必填参数缺失。"""
    issues: list[ValidationIssue] = []
    for node in graph.nodes:
        required = NODE_REQUIRED_PARAMS.get(node.kind, frozenset())
        for param_name in required:
            if param_name not in node.params:
                issues.append(
                    ValidationIssue(
                        field=f"node:{node.id}:params:{param_name}",
                        message=(
                            f"节点 {node.id}（{node.kind.value}）"
                            f"缺少必填参数: {param_name}"
                        ),
                    )
                )
    return issues


def _check_loop_assertions(
    graph: WorkflowGraph,
    available_metrics: frozenset[str],
    sandbox_available: bool,
) -> list[ValidationIssue]:
    """Loop 节点的断言可验证性校验（复用 M3 的 validate_goal）。"""
    from ariadne.spec_module.loader import derive_goal
    from ariadne.spec_module.schema import GoalSpec, Spec

    issues: list[ValidationIssue] = []
    for node in graph.nodes:
        if node.kind != NodeKind.LOOP:
            continue
        goal_data = node.params.get("goal")
        if goal_data is None:
            continue  # 缺失已在 _check_required_params 报告

        try:
            goal_spec = GoalSpec.model_validate(goal_data)
            spec = Spec(goal=goal_spec)
            goal = derive_goal(spec)
        except Exception as exc:
            issues.append(
                ValidationIssue(
                    field=f"node:{node.id}:goal",
                    message=f"Loop 节点 goal 解析失败: {exc}",
                )
            )
            continue

        report = validate_goal(
            goal,
            available_metrics=available_metrics,
            sandbox_available=sandbox_available,
        )
        for err in report.errors:
            issues.append(
                ValidationIssue(
                    field=f"node:{node.id}:goal:{err.field}",
                    message=err.message,
                )
            )
        for warn in report.warnings:
            issues.append(
                ValidationIssue(
                    field=f"node:{node.id}:goal:{warn.field}",
                    message=warn.message,
                    severity="warning",
                )
            )

    return issues


_MAX_SUBGRAPH_DEPTH = 10


def _check_subgraph_validity(
    graph: WorkflowGraph,
    available_metrics: frozenset[str],
    sandbox_available: bool,
    depth: int = 0,
) -> list[ValidationIssue]:
    """Subgraph 节点的嵌套子图递归校验。

    对每个 SUBGRAPH 节点，反序列化 params["graph"] 为 WorkflowGraph，
    递归调用 validate_graph 校验子图。深度限制防止无限递归。
    子图内部的 SUBGRAPH 节点也被递归校验。
    """
    from ariadne.graph_module.serialize import _deserialize_graph

    issues: list[ValidationIssue] = []

    for node in graph.nodes:
        if node.kind != NodeKind.SUBGRAPH:
            continue

        graph_data = node.params.get("graph")
        if graph_data is None:
            continue  # 缺失已在 _check_required_params 报告

        if depth >= _MAX_SUBGRAPH_DEPTH:
            issues.append(
                ValidationIssue(
                    field=f"node:{node.id}:params:graph",
                    message=(
                        f"子图嵌套深度超过最大值 {_MAX_SUBGRAPH_DEPTH}"
                        f"（节点 {node.id}），可能无限递归"
                    ),
                )
            )
            continue

        # 子图可能是 WorkflowGraph 或 dict（已反序列化或原始 dict）
        if isinstance(graph_data, WorkflowGraph):
            subgraph = graph_data
        elif isinstance(graph_data, dict):
            try:
                subgraph = _deserialize_graph(graph_data)
            except Exception as exc:
                issues.append(
                    ValidationIssue(
                        field=f"node:{node.id}:params:graph",
                        message=f"子图反序列化失败: {exc}",
                    )
                )
                continue
        else:
            issues.append(
                ValidationIssue(
                    field=f"node:{node.id}:params:graph",
                    message=(
                        f"graph 参数类型无效: {type(graph_data).__name__}"
                        f"（应为 dict 或 WorkflowGraph）"
                    ),
                )
            )
            continue

        # 递归校验子图（深度 +1 传播到子图的 SUBGRAPH 节点）
        sub_report = validate_graph(
            subgraph,
            available_metrics=available_metrics,
            sandbox_available=sandbox_available,
            _subgraph_depth=depth + 1,
        )
        # 加前缀标明来源子图节点
        for issue in sub_report.issues:
            issues.append(
                ValidationIssue(
                    field=f"subgraph[{node.id}].{issue.field}",
                    message=issue.message,
                    severity=issue.severity,
                )
            )

    return issues


__all__ = ["validate_graph"]
