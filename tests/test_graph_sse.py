"""Graph SSE 事件发布集成测试（阶段 3-3）。

测 GraphEventPublisher（Worker 端发布）→ Redis 频道 → 可被订阅端收到。
与 test_loop_sse 同模式：真实 Redis 不可用时跳过（pubsub 传播不 mock）。

API 层 SSE 端点（GET /graphs/runs/{id}/stream）复用 api/sse.py 的
event_stream 通用实现，其订阅-转发行为已由 test_loop_sse 覆盖，
这里不再重复。
"""

from __future__ import annotations

import asyncio
import os
from uuid import UUID

import pytest

from ariadne.worker.graph_events import (
    EVENT_NODE_STATE,
    EVENT_RUN_FINISHED,
    EVENT_RUN_STARTED,
    GraphEventPublisher,
)

REDIS_URL = os.environ.get("ARIADNE_REDIS_URL", "redis://localhost:6380/0")
RUN_ID = UUID("00000000-0000-0000-0000-000000000011")

pytestmark = pytest.mark.integration


def _redis_available() -> bool:
    try:
        import redis

        client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=2)
        try:
            return bool(client.ping())
        finally:
            client.close()
    except Exception:
        return False


requires_redis = pytest.mark.skipif(
    not _redis_available(), reason="真实 Redis 不可用（需 ARIADNE_REDIS_URL）"
)


async def _subscribe_one(channel: str) -> str | None:
    """订阅频道，收 1 条消息（带超时）。"""
    import redis.asyncio as aioredis

    client = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    pubsub = client.pubsub()
    try:
        await pubsub.subscribe(channel)
        async with asyncio.timeout(5.0):
            async for message in pubsub.listen():
                if message.get("type") == "message":
                    return message.get("data")
        return None
    finally:
        await pubsub.aclose()
        await client.aclose()


@requires_redis
@pytest.mark.asyncio
async def test_publisher_emits_node_and_run_events() -> None:
    """发布节点事件 + 起止事件，订阅端能收到。"""
    import json

    from ariadne.graph_module.executor import NodeState

    channel = f"graph:events:{RUN_ID}"

    # 先订阅后发布：pubsub 订阅未建立时发布会丢失（Redis 无持久化）
    receive_task = asyncio.create_task(_subscribe_one(channel))
    await asyncio.sleep(0.1)  # 等订阅建立

    publisher = GraphEventPublisher(REDIS_URL, str(RUN_ID))
    await publisher.publish_run_started()
    await publisher.on_node_state("input", NodeState.COMPLETED)
    await publisher.publish_run_finished("COMPLETED")
    await publisher.close()

    data = await receive_task
    assert data is not None
    payload = json.loads(data)
    assert payload["graph_run_id"] == str(RUN_ID)
    assert payload["event"] in (EVENT_RUN_STARTED, EVENT_NODE_STATE, EVENT_RUN_FINISHED)
    assert "ts" in payload


@requires_redis
@pytest.mark.asyncio
async def test_publisher_run_finished_payload() -> None:
    """终态事件带 final_state。"""
    import json

    channel = f"graph:events:{RUN_ID}"

    receive_task = asyncio.create_task(_subscribe_one(channel))
    await asyncio.sleep(0.1)

    publisher = GraphEventPublisher(REDIS_URL, str(RUN_ID))
    await publisher.publish_run_finished("FAILED")
    await publisher.close()

    data = await receive_task
    assert data is not None
    payload = json.loads(data)
    assert payload["event"] == EVENT_RUN_FINISHED
    assert payload["final_state"] == "FAILED"


@pytest.mark.asyncio
async def test_publish_failure_is_non_fatal() -> None:
    """Redis 不可达时发布失败不抛异常（尽力而为）。

    不挂在 requires_redis 上：这个测试测的正是 Redis 不可达的场景。
    """
    from ariadne.graph_module.executor import NodeState

    publisher = GraphEventPublisher("redis://localhost:1/0", str(RUN_ID))
    # 不应抛异常（_publish 内部捕获并记日志）
    await publisher.publish_run_started()
    await publisher.on_node_state("x", NodeState.COMPLETED)
    await publisher.publish_run_finished("COMPLETED")
    await publisher.close()