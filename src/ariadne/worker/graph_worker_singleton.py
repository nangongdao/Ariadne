"""GraphWorker 全局单例。

MVP 简化版：GraphWorker 作为 API 进程的全局单例，在 lifespan 中初始化。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ariadne.config import Settings
    from ariadne.storage.postgres.engine import PostgresStore
    from ariadne.worker.graph_worker import GraphWorker

_graph_worker: GraphWorker | None = None


def init_graph_worker(
    pg_factory: PostgresStore,
    settings: Settings,
) -> None:
    """初始化全局 GraphWorker（API 启动时调用）。"""
    global _graph_worker
    from ariadne.worker.graph_worker import GraphWorker

    _graph_worker = GraphWorker(pg_factory, settings)


def get_graph_worker() -> GraphWorker:
    """获取全局 GraphWorker 实例。"""
    if _graph_worker is None:
        raise RuntimeError("GraphWorker 未初始化，请在 API lifespan 中调用 init_graph_worker")
    return _graph_worker


async def shutdown_graph_worker() -> None:
    """关闭 GraphWorker（API 关闭时调用）。"""
    if _graph_worker is not None:
        await _graph_worker.shutdown()


__all__ = ["get_graph_worker", "init_graph_worker", "shutdown_graph_worker"]
