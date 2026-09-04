"""Graph 执行检查点管理（阶段 3-2）。

支持在 GraphExecutor 执行过程中保存节点完成状态，
Worker 崩溃后可从最后完成的节点恢复执行。
"""

from __future__ import annotations

import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Protocol

from ariadne.graph_module.executor import NodeState
from ariadne.utils.logging import get_logger

if TYPE_CHECKING:
    from ariadne.storage.postgres.repositories.graph_runs import GraphRunRepository

logger = get_logger(__name__)


class CheckpointSaver(Protocol):
    """检查点保存器协议。

    GraphExecutor 在每个节点完成后调用 save() 保存状态。
    """

    async def save(self, node_id: str, state: NodeState) -> None:
        """保存单个节点的执行状态。"""
        ...


class NoOpCheckpointSaver:
    """空检查点保存器（用于不需要检查点的场景）。"""

    async def save(self, node_id: str, state: NodeState) -> None:
        """不执行任何操作。"""
        pass

    async def save_with_output(
        self,
        node_id: str,
        state: NodeState,
        output: dict[str, Any] | None,
    ) -> None:
        """不执行任何操作；保留完整检查点协议形状。"""
        pass


class GraphRunCheckpointSaver:
    """基于 GraphRunRepository 的检查点保存器。

    每个节点完成后调用 repo.save_checkpoint() 增量更新 node_states。
    """

    def __init__(
        self,
        graph_run_id: uuid.UUID,
        repo: GraphRunRepository,
        *,
        initial_states: dict[str, str] | None = None,
        initial_outputs: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._graph_run_id = graph_run_id
        self._repo = repo
        # 恢复时必须把旧快照作为基线，否则第一次新节点保存会覆盖此前状态。
        self._node_states = dict(initial_states or {})
        self._node_outputs = {
            node_id: dict(output)
            for node_id, output in (initial_outputs or {}).items()
        }

    async def save(self, node_id: str, state: NodeState) -> None:
        """保存节点状态到数据库（兼容旧 CheckpointSaver 协议）。"""
        await self.save_with_output(node_id, state, None)

    async def save_with_output(
        self,
        node_id: str,
        state: NodeState,
        output: dict[str, Any] | None,
    ) -> None:
        """原子保存节点状态与输出到数据库。"""
        self._node_states[node_id] = state.value
        if state is NodeState.COMPLETED and output is not None:
            self._node_outputs[node_id] = dict(output)
        elif state is not NodeState.COMPLETED:
            self._node_outputs.pop(node_id, None)

        try:
            await self._repo.save_checkpoint(
                self._graph_run_id,
                self._node_states,
                self._node_outputs,
            )
            await self._repo.session.commit()
            logger.debug(
                f"检查点已保存: graph_run={self._graph_run_id}, "
                f"node={node_id}, state={state.value}"
            )
        except Exception:
            # 数据库异常会让当前事务进入 failed 状态；若不 rollback，后续节点
            # 即使执行成功也无法继续保存或 finish。
            with suppress(Exception):
                await self._repo.session.rollback()
            logger.exception(
                f"保存检查点失败: graph_run={self._graph_run_id}, node={node_id}"
            )
            # 检查点保存失败不应中断图执行
            # 只记录日志，继续执行

    def get_completed_nodes(self) -> set[str]:
        """返回已完成节点集合（用于恢复时跳过）。"""
        return {
            node_id
            for node_id, state in self._node_states.items()
            if state == NodeState.COMPLETED.value
        }

    def get_node_outputs(self) -> dict[str, dict[str, Any]]:
        """返回当前已保存输出的防御性副本。"""
        return {
            node_id: dict(output)
            for node_id, output in self._node_outputs.items()
        }


async def load_checkpoint(
    graph_run_id: uuid.UUID,
    repo: GraphRunRepository,
) -> dict[str, str] | None:
    """加载检查点数据。

    返回 node_states 字典或 None（无检查点）。
    """
    try:
        return await repo.get_checkpoint(graph_run_id)
    except Exception:
        logger.exception(f"加载检查点失败: graph_run={graph_run_id}")
        return None


async def load_checkpoint_outputs(
    graph_run_id: uuid.UUID,
    repo: GraphRunRepository,
) -> dict[str, dict[str, Any]] | None:
    """加载检查点中用于恢复数据流的节点输出。"""
    try:
        return await repo.get_checkpoint_outputs(graph_run_id)
    except Exception:
        logger.exception(f"加载检查点输出失败: graph_run={graph_run_id}")
        return None


def should_skip_node(
    node_id: str,
    checkpoint: dict[str, str] | None,
) -> bool:
    """判断节点是否应该跳过（已在检查点中完成）。

    Args:
        node_id: 节点 ID
        checkpoint: 检查点数据（node_states）

    Returns:
        True 表示节点已完成，应跳过执行
    """
    if checkpoint is None:
        return False

    node_state = checkpoint.get(node_id)
    return node_state == NodeState.COMPLETED.value


__all__ = [
    "CheckpointSaver",
    "GraphRunCheckpointSaver",
    "NoOpCheckpointSaver",
    "load_checkpoint",
    "load_checkpoint_outputs",
    "should_skip_node",
]
