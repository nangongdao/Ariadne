"""GraphQueue 全局单例（阶段 2）。

API 创建 graph_run 后入队到 Redis，Worker 从队列消费。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ariadne.config import Settings
    from ariadne.worker.graph_queue import GraphQueue

_graph_queue: GraphQueue | None = None


def init_graph_queue(settings: Settings) -> None:
    """初始化全局 GraphQueue（API 启动时调用）。"""
    global _graph_queue
    from ariadne.worker.graph_queue import GraphQueue

    _graph_queue = GraphQueue(settings.redis)


def get_graph_queue() -> GraphQueue:
    """获取全局 GraphQueue 实例。"""
    if _graph_queue is None:
        raise RuntimeError("GraphQueue 未初始化，请先调用 init_graph_queue()")
    return _graph_queue


async def close_graph_queue() -> None:
    """关闭全局 GraphQueue（API 关闭时调用）。"""
    global _graph_queue
    if _graph_queue is not None:
        await _graph_queue.close()
        _graph_queue = None


__all__ = ["close_graph_queue", "get_graph_queue", "init_graph_queue"]
