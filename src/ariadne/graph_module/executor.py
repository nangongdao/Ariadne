"""DAG 执行器 —— 拓扑排序 + 并发调度 + 条件分支。

执行策略（docs/M5 §4.3）：
1. 用 graphlib.TopologicalSorter 做拓扑排序，同层无依赖节点并发执行
2. 节点间数据通过 dict[port_name, value] 传递
3. Branch 节点返回 {"__route": "branch_name"}，执行器据此跳过不匹配的下游
4. 节点失败 → 该节点标记 failed，其所有下游标记 skipped（fail-fast）

阶段 3-2 新增：
- 支持检查点保存（每个节点完成后调用 checkpoint_saver.save()）
- 支持从检查点恢复（跳过已完成节点）

TopologicalSorter.prepare() + get_ready() 是标准库自带的"层级调度"原语，
天然支持并发：同一层（get_ready 返回的一批）互相无依赖，可 asyncio.gather。
done(node) 通知依赖完成后，下一层节点进入 get_ready 队列。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import StrEnum
from graphlib import CycleError, TopologicalSorter
from typing import Any, Protocol

from ariadne.graph_module.models import NodeBase, NodeKind, WorkflowGraph
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)


class NodeState(StrEnum):
    """节点执行状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


# Branch 节点路由约定：executor 返回此 key 指定激活的下游分支
ROUTE_KEY = "__route"


@dataclass(frozen=True)
class NodeExecutionContext:
    """传给每个节点 executor 的执行上下文。

    inputs 是上游节点输出按 edge 映射后的 {port_name: value}。
    node 是当前节点定义。graph 是完整图引用（分支节点需查 branches）。
    """

    node: NodeBase
    inputs: dict[str, Any] = field(default_factory=dict)
    graph: WorkflowGraph | None = None


class NodeExecutor(Protocol):
    """节点执行器协议。

    每种 NodeKind 对应一个 executor 实例。run() 时注入到 node_executors dict。
    """

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        """执行节点，返回 {output_port_name: value}。

        Branch 节点返回 {ROUTE_KEY: branch_name} 指定路由。
        """
        ...


class CheckpointSaver(Protocol):
    """检查点保存器协议（阶段 3-2）。

    GraphExecutor 在每个节点完成后调用 save() 保存状态。
    """

    async def save(self, node_id: str, state: NodeState) -> None:
        """保存单个节点的执行状态。"""
        ...


class NodeEventCallback(Protocol):
    """节点状态事件回调（阶段 3-3，SSE 推送用）。

    GraphExecutor 在每个节点状态落定（completed/failed/skipped）后调用。
    Worker 把它桥接到 Redis pub/sub，API 的 SSE 端点订阅转发给前端。
    调用是尽力而为的：发布失败只记日志，不阻塞图执行。
    """

    async def on_node_state(self, node_id: str, state: NodeState) -> None:
        """节点状态落定。"""
        ...


@dataclass(frozen=True)
class ExecutionResult:
    """图执行结果。"""

    outputs: dict[str, Any] = field(default_factory=dict)
    node_states: dict[str, NodeState] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class GraphExecutor:
    """DAG 执行器 —— 拓扑排序 + 并发调度 + 条件分支路由 + 检查点恢复（阶段 3-2）。"""

    async def run(
        self,
        graph: WorkflowGraph,
        *,
        inputs: dict[str, Any] | None = None,
        node_executors: dict[NodeKind, NodeExecutor],
        checkpoint_saver: CheckpointSaver | None = None,
        completed_nodes: set[str] | None = None,
        completed_outputs: dict[str, dict[str, Any]] | None = None,
        node_event_callback: NodeEventCallback | None = None,
    ) -> ExecutionResult:
        """执行图，返回每个节点的状态和最终输出。

        同层节点用 asyncio.gather 并发。节点失败 → 下游 skipped（fail-fast）。

        阶段 3-2 新增参数：
            checkpoint_saver: 检查点保存器（每个节点完成后调用）
            completed_nodes: 已完成节点集合（从检查点恢复时使用，跳过这些节点）
            completed_outputs: 已完成节点的持久化输出。Worker 恢复时显式传入；
                若某个 completed 节点没有输出，则为保证数据流正确会重新执行。

        阶段 3-3 新增参数：
            node_event_callback: 节点状态变化回调（SSE 事件发布端，每个节点
                状态落定后调用一次，收到 (node_id, state)）
        """
        if graph.is_empty:
            return ExecutionResult()

        # 防御性环检测（validate_graph 也会做，这里做二次保险）
        try:
            list(_static_order(graph))
        except CycleError:
            return ExecutionResult(
                node_states={n.id: NodeState.SKIPPED for n in graph.nodes},
                errors=["图包含环，无法执行"],
            )

        node_map = {n.id: n for n in graph.nodes}
        states: dict[str, NodeState] = {n.id: NodeState.PENDING for n in graph.nodes}
        node_outputs: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        skipped_nodes: set[str] = set()
        failed_nodes: set[str] = set()

        # 阶段 3-2：从检查点恢复。生产恢复必须同时有节点输出；只有状态
        # 无法为下游重建输入。completed_outputs=None 保留旧调用方的简化语义。
        resumable_nodes = set(completed_nodes or ())
        if completed_outputs is not None:
            missing_outputs = resumable_nodes.difference(completed_outputs)
            if missing_outputs:
                logger.warning(
                    "检查点缺少 %d 个已完成节点的输出，将重新执行: %s",
                    len(missing_outputs),
                    ", ".join(sorted(missing_outputs)),
                )
                resumable_nodes.difference_update(missing_outputs)

        if resumable_nodes:
            for node_id in resumable_nodes:
                if node_id in states:
                    states[node_id] = NodeState.COMPLETED
                    if completed_outputs is None:
                        restored: dict[str, Any] = {}
                    else:
                        restored_value = completed_outputs.get(node_id)
                        restored = (
                            dict(restored_value)
                            if isinstance(restored_value, dict)
                            else {}
                        )
                    node_outputs[node_id] = restored
                    logger.info(f"从检查点恢复: 节点 {node_id} 已完成，跳过执行")

            # 已完成的 Branch 不会再调用 executor，因此需用持久化的 __route
            # 重放分支选择，否则恢复后未选中的分支也会被执行。
            for node_id in resumable_nodes:
                node = node_map.get(node_id)
                restored = node_outputs.get(node_id, {})
                if node is not None and node.kind is NodeKind.BRANCH and restored:
                    _apply_branch_routing(
                        node_id,
                        restored,
                        graph,
                        node_map,
                        skipped_nodes,
                        states,
                    )

        # 源节点（无入边）的初始输入来自 inputs 参数
        incoming_map = _build_incoming_map(graph)

        ts: TopologicalSorter[str] = TopologicalSorter()
        predecessors = _build_predecessors(graph)
        for node_id, preds in predecessors.items():
            ts.add(node_id, *preds)
        ts.prepare()

        while True:
            ready = ts.get_ready()
            if not ready:
                if ts.is_active():
                    # 有节点在执行中但没新节点 ready —— 等待
                    continue
                break

            # 过滤掉被 skip 的节点（上游 fail 或分支未选中或已从检查点恢复）
            runnable: list[str] = []
            for node_id in ready:
                # 阶段 3-2：跳过已完成节点
                if node_id in resumable_nodes:
                    ts.done(node_id)
                    continue

                if node_id in skipped_nodes or node_id in failed_nodes:
                    # 这些节点不需要执行，直接标记并通知拓扑排序器
                    states[node_id] = NodeState.SKIPPED
                    await _emit_node_event(
                        node_event_callback, node_id, NodeState.SKIPPED
                    )
                    ts.done(node_id)
                    continue
                runnable.append(node_id)

            if not runnable:
                continue

            # 并发执行同层可运行节点
            tasks: list[Any] = []
            task_node_ids: list[str] = []
            for node_id in runnable:
                states[node_id] = NodeState.RUNNING
                node = node_map[node_id]
                node_inputs = _collect_inputs(
                    node_id, node, incoming_map, node_outputs, inputs or {}
                )
                ctx = NodeExecutionContext(
                    node=node, inputs=node_inputs, graph=graph
                )
                executor = node_executors.get(node.kind)
                if executor is None:
                    states[node_id] = NodeState.FAILED
                    errors.append(
                        f"节点 {node_id}（{node.kind.value}）没有注册 executor"
                    )
                    failed_nodes.add(node_id)
                    _cascade_skip(
                        node_id, graph, failed_nodes, skipped_nodes, states
                    )
                    await _emit_node_event(
                        node_event_callback, node_id, NodeState.FAILED
                    )
                    ts.done(node_id)
                    continue
                tasks.append(self._execute_node(node_id, ctx, executor))
                task_node_ids.append(node_id)

            if not task_node_ids:
                continue

            # 并发等待
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for node_id, result in zip(task_node_ids, results, strict=True):
                if isinstance(result, BaseException):
                    states[node_id] = NodeState.FAILED
                    errors.append(f"节点 {node_id} 执行失败: {result}")
                    failed_nodes.add(node_id)
                    _cascade_skip(
                        node_id, graph, failed_nodes, skipped_nodes, states
                    )
                    # 阶段 3-2：保存失败节点检查点
                    if checkpoint_saver:
                        await _save_node_checkpoint(
                            checkpoint_saver,
                            node_id,
                            NodeState.FAILED,
                            None,
                        )
                    await _emit_node_event(
                        node_event_callback, node_id, NodeState.FAILED
                    )
                    ts.done(node_id)
                    continue

                states[node_id] = NodeState.COMPLETED
                node_outputs[node_id] = result

                # 阶段 3-2：保存完成节点检查点
                if checkpoint_saver:
                    await _save_node_checkpoint(
                        checkpoint_saver,
                        node_id,
                        NodeState.COMPLETED,
                        result,
                    )

                await _emit_node_event(
                    node_event_callback, node_id, NodeState.COMPLETED
                )

                # Branch 路由：跳过未选中的下游分支
                if node_map[node_id].kind == NodeKind.BRANCH:
                    _apply_branch_routing(
                        node_id,
                        result,
                        graph,
                        node_map,
                        skipped_nodes,
                        states,
                    )

                ts.done(node_id)

        final_outputs = _collect_terminal_outputs(graph, node_outputs)
        return ExecutionResult(
            outputs=final_outputs,
            node_states=states,
            errors=errors,
        )

    async def _execute_node(
        self,
        node_id: str,
        ctx: NodeExecutionContext,
        executor: NodeExecutor,
    ) -> dict[str, Any]:
        """执行单个节点，捕获异常。"""
        try:
            return await executor.execute(ctx)
        except Exception as exc:
            logger.warning("节点 %s 执行异常: %s", node_id, exc)
            raise


async def _emit_node_event(
    callback: NodeEventCallback | None,
    node_id: str,
    state: NodeState,
) -> None:
    """回调节点状态事件。尽力而为：失败只记日志，不阻塞图执行。"""
    if callback is None:
        return
    try:
        await callback.on_node_state(node_id, state)
    except Exception as exc:
        logger.warning(
            "node event callback failed",
            extra={"node_id": node_id, "state": state.value, "error": str(exc)},
        )


# ---------- 辅助函数 ----------


async def _save_node_checkpoint(
    saver: CheckpointSaver,
    node_id: str,
    state: NodeState,
    output: dict[str, Any] | None,
) -> None:
    """保存检查点，并兼容只实现旧版 ``save(node_id, state)`` 的 saver。"""
    save_with_output = getattr(saver, "save_with_output", None)
    if callable(save_with_output):
        await save_with_output(node_id, state, output)
        return
    await saver.save(node_id, state)


def _static_order(graph: WorkflowGraph) -> list[str]:
    """获取拓扑序（用于防御性环检测）。"""
    ts: TopologicalSorter[str] = TopologicalSorter()
    predecessors = _build_predecessors(graph)
    for node_id, preds in predecessors.items():
        ts.add(node_id, *preds)
    return list(ts.static_order())


def _build_predecessors(graph: WorkflowGraph) -> dict[str, set[str]]:
    """构建每个节点的直接前驱集合。"""
    preds: dict[str, set[str]] = {n.id: set() for n in graph.nodes}
    for edge in graph.edges:
        if edge.source in preds and edge.target in preds:
            preds[edge.target].add(edge.source)
    return preds


def _build_incoming_map(graph: WorkflowGraph) -> dict[str, list[tuple[str, str, str]]]:
    """构建每个节点的入边列表: [(source_node, source_port, target_port)]。"""
    incoming: dict[str, list[tuple[str, str, str]]] = {
        n.id: [] for n in graph.nodes
    }
    for edge in graph.edges:
        if edge.target in incoming:
            incoming[edge.target].append(
                (edge.source, edge.source_port, edge.target_port)
            )
    return incoming


def _collect_inputs(
    node_id: str,
    node: NodeBase,
    incoming_map: dict[str, list[tuple[str, str, str]]],
    node_outputs: dict[str, dict[str, Any]],
    initial_inputs: dict[str, Any],
) -> dict[str, Any]:
    """收集节点的输入：上游输出按 edge 映射到输入端口名。

    源节点（无入边）从 initial_inputs 按 port name 取值。
    """
    inputs: dict[str, Any] = {}
    incoming = incoming_map.get(node_id, [])

    if not incoming:
        # 源节点：从 initial_inputs 按 port name 取
        for port in node.inputs:
            if port.name in initial_inputs:
                inputs[port.name] = initial_inputs[port.name]
        return inputs

    for source_node, source_port, target_port in incoming:
        source_output = node_outputs.get(source_node, {})
        if source_port in source_output:
            inputs[target_port] = source_output[source_port]

    return inputs


def _cascade_skip(
    failed_node: str,
    graph: WorkflowGraph,
    failed_nodes: set[str],
    skipped_nodes: set[str],
    states: dict[str, NodeState],
) -> None:
    """递归标记失败节点的所有下游为 skipped。"""
    # 构建邻接表
    children: dict[str, list[str]] = {n.id: [] for n in graph.nodes}
    for edge in graph.edges:
        if edge.source in children:
            children[edge.source].append(edge.target)

    queue = list(children.get(failed_node, []))
    while queue:
        child = queue.pop()
        if child in skipped_nodes or child in failed_nodes:
            continue
        skipped_nodes.add(child)
        states[child] = NodeState.SKIPPED
        queue.extend(children.get(child, []))


def _apply_branch_routing(
    branch_node_id: str,
    result: dict[str, Any],
    graph: WorkflowGraph,
    node_map: dict[str, NodeBase],
    skipped_nodes: set[str],
    states: dict[str, NodeState],
) -> None:
    """Branch 节点路由：根据 __route 跳过未选中的下游分支。

    Branch 节点的 params["branches"] 格式: {branch_name: target_node_id}。
    executor 返回 {"__route": "branch_name"} 指定选中分支。
    未选中的分支目标节点及其下游被 skip。
    """
    if ROUTE_KEY not in result:
        return  # 无路由信息，不做跳过

    selected_branch = result[ROUTE_KEY]
    branch_node = node_map.get(branch_node_id)
    if branch_node is None:
        return

    branches = branch_node.params.get("branches", {})
    if not isinstance(branches, dict):
        return

    # 找到 branch 节点的所有直接下游节点
    branch_targets: set[str] = set()
    for edge in graph.edges:
        if edge.source == branch_node_id:
            branch_targets.add(edge.target)

    # 确定选中分支的目标节点
    selected_target = branches.get(selected_branch)
    if selected_target is not None and selected_target in branch_targets:
        branch_targets.discard(selected_target)

    # 跳过未选中的分支目标及其下游
    for target in branch_targets:
        if target not in skipped_nodes:
            skipped_nodes.add(target)
            states[target] = NodeState.SKIPPED
            _cascade_skip(
                target, graph, set(), skipped_nodes, states
            )


def _collect_terminal_outputs(
    graph: WorkflowGraph,
    node_outputs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """收集终端节点（无出边）的输出。"""
    has_outgoing: set[str] = set()
    for edge in graph.edges:
        has_outgoing.add(edge.source)

    outputs: dict[str, Any] = {}
    for node in graph.nodes:
        if node.id not in has_outgoing and node.id in node_outputs:
            outputs[node.id] = node_outputs[node.id]
    return outputs


__all__ = [
    "ROUTE_KEY",
    "ExecutionResult",
    "GraphExecutor",
    "NodeEventCallback",
    "NodeExecutionContext",
    "NodeExecutor",
    "NodeState",
]
