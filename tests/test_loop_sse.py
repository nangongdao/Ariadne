"""Loop SSE 事件流集成测试。

测 `_event_stream` 协程（不测 HTTP 层）：发布到 Redis 频道 → 流式产出
`data:` 帧；空闲时发心跳不断流。

依赖真实 Redis。用 `ARIADNE_REDIS_URL` 环境变量，连不上时跳过 ——
pubsub 的传播行为（Windows Redis 5.0 有亚秒延迟）不 mock，这正是
sse.py 用 listen() 阻塞读取的原因。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from uuid import UUID

import pytest

from ariadne.api.sse import _event_stream

REDIS_URL = os.environ.get("ARIADNE_REDIS_URL", "redis://localhost:6380/0")
LOOP_ID = UUID("00000000-0000-0000-0000-000000000010")


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


pytestmark = [
    pytest.mark.skipif(
        not _redis_available(), reason="真实 Redis 不可用（需 ARIADNE_REDIS_URL）"
    ),
    pytest.mark.integration,
]


async def _collect(stream: AsyncIterator[str], count: int) -> list[str]:
    """取流中前 count 个非空帧（带超时，防挂死）。"""
    frames: list[str] = []
    async with asyncio.timeout(5.0):
        async for frame in stream:
            if frame.strip():
                frames.append(frame)
                if len(frames) >= count:
                    break
    return frames


class TestEventStream:
    async def test_receives_published_event(self) -> None:
        """发布到频道 → 流产出 `data: <json>` 帧。"""
        import redis.asyncio as aioredis

        publisher = aioredis.from_url(REDIS_URL, decode_responses=True)
        stream = _event_stream(REDIS_URL, LOOP_ID)
        task = asyncio.create_task(_collect(stream, 1))
        # 订阅生效需要时间（Windows Redis 5.0 传播延迟）
        await asyncio.sleep(0.8)
        payload = json.dumps(
            {"event": "START", "loop_id": str(LOOP_ID), "ts": "2026-08-26T00:00:00Z"}
        )
        await publisher.publish(f"loop:events:{LOOP_ID}", payload)
        frames = await task
        await publisher.aclose()
        assert len(frames) == 1
        assert frames[0].startswith("data: ")
        assert "START" in frames[0]

    async def test_stream_cancels_cleanly(self) -> None:
        """无事件时阻塞读取；取消后流能优雅关闭（前端断连不泄漏连接）。"""
        import redis.asyncio as aioredis

        publisher = aioredis.from_url(REDIS_URL, decode_responses=True)
        stream = _event_stream(REDIS_URL, LOOP_ID)

        async def consume() -> int:
            count = 0
            async for _frame in stream:
                count += 1
            return count

        task = asyncio.create_task(consume())
        await asyncio.sleep(1.0)  # 订阅 + 进入阻塞读取
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # 取消后发布不应影响（连接已关）
        await publisher.publish(
            f"loop:events:{LOOP_ID}", json.dumps({"event": "LATE"})
        )
        await publisher.aclose()

    async def test_after_event_stream_continues(self) -> None:
        """事件后流继续（不因一条消息关闭）——前端重连不需重开。"""
        import redis.asyncio as aioredis

        publisher = aioredis.from_url(REDIS_URL, decode_responses=True)
        stream = _event_stream(REDIS_URL, LOOP_ID)
        task = asyncio.create_task(_collect(stream, 2))
        await asyncio.sleep(0.8)
        for i in range(2):
            await publisher.publish(
                f"loop:events:{LOOP_ID}",
                json.dumps({"event": f"E{i}", "loop_id": str(LOOP_ID)}),
            )
            await asyncio.sleep(0.3)
        frames = await task
        await publisher.aclose()
        assert len(frames) == 2
        assert "E0" in frames[0] and "E1" in frames[1]