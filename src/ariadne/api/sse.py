"""Loop SSE 实时事件流。

`GET /v1/loops/{id}/stream`：订阅 Redis 频道 `loop:events:{loop_id}`，
把 Worker 广播的事件以 SSE 格式推送前端（进化视图实时更新）。

设计（M3-spec 6.5）：
- 事件源是 Redis pub/sub（Worker 发布，API 订阅转发）——多 Worker
  多 API 实例天然可扩
- 每 15s 发心跳注释行（`: ping`），保持连接存活（代理超时兜底）
- 断线：前端重连后从当前状态重新拉详情（路由 GET /loops/{id}），
  不做事件重放（M3 范围，历史事件经 iterations 端点取）

不引 sse-starlette：SSE 格式简单（`data: ...\n\n`），手写依赖更少。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ariadne.api.deps import ProjectId, TenantPg
from ariadne.api.errors import NotFoundError
from ariadne.config import Settings
from ariadne.storage.postgres.repositories.loop_runs import LoopRunNotFoundError, LoopRunRepository

router = APIRouter(tags=["loops"])

# 心跳间隔。触发代理/负载均衡器的空闲超时兜底
HEARTBEAT_SECONDS = 15
# 事件频道前缀。发布端在 worker/loop_worker.py（RedisEventSink）
CHANNEL_PREFIX = "loop:events:"


async def event_stream(redis_url: str, channel: str) -> AsyncIterator[str]:
    """订阅 Redis 频道，逐事件 yield SSE 帧（通用实现）。

    用 listen() 阻塞读取而非 get_message 轮询：Windows Redis 5.0 的
    PUBLISH→SUBSCRIBE 传播有亚秒延迟，轮询会让事件延迟 0-5s（心跳间隔）。
    阻塞读取 + 心跳超时：事件零延迟，空闲时每 HEARTBEAT_SECONDS 发心跳。

    Loop 与 Graph 共用：channel 由调用方拼好（loop:events:{id} /
    graph:events:{id}）。
    """
    import asyncio

    import redis.asyncio as aioredis

    redis_client = aioredis.from_url(  # type: ignore[no-untyped-call]
        redis_url, encoding="utf-8", decode_responses=True
    )
    pubsub = redis_client.pubsub()
    try:
        await pubsub.subscribe(channel)
        listener = pubsub.listen()
        while True:
            try:
                message = await asyncio.wait_for(
                    anext(listener), timeout=HEARTBEAT_SECONDS
                )
            except TimeoutError:
                # 无事件超过心跳间隔：发心跳保活
                yield ": ping\n\n"
                continue
            if message.get("type") != "message":
                continue
            data = message.get("data")
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            if isinstance(data, str):
                yield f"data: {data}\n\n"
    finally:
        await pubsub.aclose()
        await redis_client.aclose()


async def _event_stream(redis_url: str, loop_id: UUID) -> AsyncIterator[str]:
    """Loop 专用包装（保留私有签名，测试兼容）。"""
    async for frame in event_stream(redis_url, f"{CHANNEL_PREFIX}{loop_id}"):
        yield frame


@router.get(
    "/loops/{loop_id}/stream",
    summary="Loop 实时事件流（SSE）",
)
async def stream_loop_events(
    loop_id: UUID,
    project_id: ProjectId,
    pg: TenantPg,
    request: Request,
) -> StreamingResponse:
    """SSE 实时推送 Loop 状态变化。

    返回 `text/event-stream`。前端用 EventSource 订阅。
    轮次详情（每轮的 score/failed_ids）经 GET /loops/{id}/iterations 拉取。
    """
    # 校验 loop 存在（404 提前返回，而非开一个永不结束的流）
    async with pg.session() as session:
        try:
            await LoopRunRepository(session).get(
                project_id=project_id, loop_id=loop_id
            )
        except LoopRunNotFoundError as exc:
            raise NotFoundError(str(exc), loop_id=str(loop_id)) from exc

    settings: Settings = request.app.state.settings

    async def generate() -> AsyncIterator[str]:
        async for frame in _event_stream(settings.redis.url, loop_id):
            yield frame

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关掉 nginx 缓冲，保证实时
        },
    )


__all__ = [
    "event_stream",
    "router",
    "stream_loop_events",
]