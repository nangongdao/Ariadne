"""Graph 事件发布 —— SSE 实时推送的事件源。

GraphWorkerV2 在节点状态落定 / 图完成时，经 Redis pub/sub 频道
`graph:events:{graph_run_id}` 发布事件；API 的 SSE 端点订阅同一频道
转发给浏览器。多 Worker 多 API 实例天然可扩（与 Loop 同设计，见
api/sse.py 与 worker/loop_worker.py::RedisEventSink）。

事件载荷：{event, graph_run_id, node_id?, state?, ts}。
前端据此渲染节点进度视图（节点变绿/变红）与最终状态。

发布是尽力而为：Redis 不可用只记日志，不阻塞图执行。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ariadne.graph_module.executor import NodeEventCallback, NodeState
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

# 事件频道前缀。API 的 SSE 端点订阅（见 api/routers/graphs.py）
CHANNEL_PREFIX = "graph:events:"

# 事件类型
EVENT_NODE_STATE = "node_state"  # 节点状态落定（payload 带 node_id/state）
EVENT_RUN_FINISHED = "run_finished"  # 图执行完成（payload 带 final_state）
EVENT_RUN_STARTED = "run_started"  # 图执行开始


def _payload(
    event: str, graph_run_id: str, **extra: Any
) -> str:
    """构造 JSON 事件串。字段少，前端解析不需要 schema 校验。"""
    data = {
        "event": event,
        "graph_run_id": graph_run_id,
        "ts": datetime.now(UTC).isoformat(),
        **extra,
    }
    return json.dumps(data, ensure_ascii=False)


class GraphEventPublisher(NodeEventCallback):
    """节点事件 → Redis pub/sub 发布（阶段 3-3）。

    实现 NodeEventCallback 协议：GraphExecutor 每节点状态落定调用一次
    on_node_state，这里发布到 `graph:events:{graph_run_id}` 频道。

    Redis 客户端延迟建连（首次发布时），与 Loop RedisEventSink 一致。
    """

    def __init__(self, redis_url: str, graph_run_id: str) -> None:
        self._redis_url = redis_url
        self._graph_run_id = graph_run_id
        self._redis: Any | None = None

    async def _client(self) -> Any:
        if self._redis is None:
            import redis.asyncio as aioredis

            self._redis = aioredis.from_url(  # type: ignore[no-untyped-call]
                self._redis_url, encoding="utf-8", decode_responses=True
            )
        return self._redis

    async def on_node_state(self, node_id: str, state: NodeState) -> None:
        """节点状态落定 → 发布 node_state 事件。"""
        await self._publish(
            EVENT_NODE_STATE, node_id=node_id, state=state.value
        )

    async def publish_run_started(self) -> None:
        """图执行开始。"""
        await self._publish(EVENT_RUN_STARTED)

    async def publish_run_finished(self, final_state: str) -> None:
        """图执行完成（终态 COMPLETED/FAILED/CANCELLED）。"""
        await self._publish(EVENT_RUN_FINISHED, final_state=final_state)

    async def _publish(self, event: str, **extra: Any) -> None:
        try:
            client = await self._client()
            await client.publish(
                f"{CHANNEL_PREFIX}{self._graph_run_id}",
                _payload(event, self._graph_run_id, **extra),
            )
        except Exception as exc:
            logger.warning(
                "graph event publish failed",
                extra={
                    "graph_run_id": self._graph_run_id,
                    "event": event,
                    "error": str(exc),
                },
            )

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


__all__ = [
    "CHANNEL_PREFIX",
    "EVENT_NODE_STATE",
    "EVENT_RUN_FINISHED",
    "EVENT_RUN_STARTED",
    "GraphEventPublisher",
]