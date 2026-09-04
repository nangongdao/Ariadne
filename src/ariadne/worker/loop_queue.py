"""Loop 任务队列 —— Redis Streams 消费者组封装。

与 SpanQueue 同构（消费者组 + ACK + 可见性超时），这正是 M3-spec 6.2
的既定设计：

- API 创建后 XADD 入队（载荷是 loop_id，Goal 已存 Postgres）
- Worker 用 XREADGROUP 从组内读任务；处理完 XACK
- 崩溃的 Worker：任务未 ACK，超过可见性超时后由 XAUTOCLAIM 回收，
  其他 Worker 接管（配合 Postgres 租约双层保险）
- 长任务用 Postgres 租约续期，避免"读走后处理中"被误回收

可见性超时选 90s：Loop 单轮通常远小于此；超时后回收的任务
会被新 Worker 从检查点续跑，不重复已有轮次。
"""

from __future__ import annotations

from typing import Final

import redis.asyncio as aioredis

from ariadne.config import RedisSettings
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

_PAYLOAD_FIELD: Final = "loop_id"
_PROJECT_FIELD: Final = "project_id"
_BUSYGROUP: Final = "BUSYGROUP"
# 认领后若长时间未 ACK（说明 Worker 崩了），其他 Worker 回收
RECLAIM_MIN_IDLE_MS: Final = 90_000
# 单个消费者一轮最多认领几个 Loop（避免一台机器吃太多长任务）
_MAX_CLAIM: Final = 3


class LoopQueue:
    """Loop 任务队列。"""

    def __init__(
        self,
        settings: RedisSettings,
        *,
        stream_key: str = "q:loop",
        consumer_group: str = "loop-workers",
    ) -> None:
        self._settings = settings
        self._stream_key = stream_key
        self._consumer_group = consumer_group
        self._redis: aioredis.Redis | None = None

    async def connect(self) -> None:
        if self._redis is None:
            self._redis = aioredis.from_url(  # type: ignore[no-untyped-call]
                self._settings.url, encoding="utf-8", decode_responses=True
            )

    @property
    def redis(self) -> aioredis.Redis:
        if self._redis is None:
            raise RuntimeError("LoopQueue 未连接，请先 await connect()")
        return self._redis

    async def ping(self) -> bool:
        try:
            await self.connect()
            return bool(await self.redis.ping())
        except Exception as exc:
            logger.warning("redis ping failed", extra={"error": str(exc)})
            return False

    async def ensure_group(self) -> None:
        """幂等创建消费者组。mkstream=True 让流不存在时一并创建。"""
        await self.connect()
        try:
            await self.redis.xgroup_create(
                self._stream_key,
                self._consumer_group,
                id="0",
                mkstream=True,
            )
        except aioredis.ResponseError as exc:
            if _BUSYGROUP not in str(exc):
                raise

    async def enqueue(self, loop_id: str, project_id: str) -> str:
        """提交一个待处理 Loop。

        载荷带 project_id：Worker 读 loop_runs 前要先设 RLS 变量，否则
        RLS 一生效就查不到行。这个值是不可信声明 —— Worker 拿它设变量后
        仍走 RLS 查询，声明错了只会查不到行（任务跳过），不会跨租户拿到数据。
        """
        await self.connect()
        message_id = await self.redis.xadd(
            self._stream_key,
            {_PAYLOAD_FIELD: loop_id, _PROJECT_FIELD: project_id},
            maxlen=100_000,  # 有界：等待处理的任务不会无限堆积
            approximate=True,
        )
        return str(message_id)

    async def claim(
        self, consumer: str, *, count: int = _MAX_CLAIM, block_ms: int = 1000
    ) -> list[tuple[str, str, str]]:
        """从消费者组认领任务，返回 [(message_id, loop_id, project_id)]。

        处理完成后用 message_id ACK；崩溃时消息留 pending，
        超时后由其他 Worker 经 XAUTOCLAIM 回收（配 Postgres 租约双保险）。
        """
        await self.connect()
        # 周期回收崩溃 Worker 遗留的未 ACK 消息。
        # 用 XPENDING + XCLAIM（Redis 5.0 兼容；XAUTOCLAIM 需 6.2+）：
        # 取 pending 列表里 idle 超时的消息 ID，再逐个 XCLAIM 认领。
        fresh: list[tuple[str, str, str]] = []
        try:
            pending = await self.redis.xpending_range(
                self._stream_key,
                self._consumer_group,
                min="-",
                max="+",
                count=count,
            )
            stale_ids = [
                p["id"]
                for p in pending
                if int(p.get("time_since_delivery", 0)) > RECLAIM_MIN_IDLE_MS
            ]
            if stale_ids:
                reclaimed = await self.redis.xclaim(
                    self._stream_key,
                    self._consumer_group,
                    consumer,
                    min_idle_time=RECLAIM_MIN_IDLE_MS,
                    message_ids=stale_ids,
                )
                for message_id, fields in reclaimed:
                    fresh.append((
                        str(message_id),
                        str(fields.get(_PAYLOAD_FIELD, "")),
                        str(fields.get(_PROJECT_FIELD, "")),
                    ))
        except aioredis.ResponseError:
            # 消费者组不存在时跳过回收，新任务仍可读
            pass
        # 新任务
        response = await self.redis.xreadgroup(
            self._consumer_group,
            consumer,
            {self._stream_key: ">"},
            count=count,
            block=block_ms,
        )
        for _stream, entries in response or []:
            for message_id, fields in entries:
                fresh.append((
                    str(message_id),
                    str(fields.get(_PAYLOAD_FIELD, "")),
                    str(fields.get(_PROJECT_FIELD, "")),
                ))
        # 去重：同一 loop_id 可能既被回收又新读
        seen: set[str] = set()
        unique: list[tuple[str, str, str]] = []
        for message_id, loop_id, project_id in fresh:
            if loop_id and loop_id not in seen:
                seen.add(loop_id)
                unique.append((message_id, loop_id, project_id))
        return unique

    async def ack(self, message_id: str) -> None:
        """ACK 已完成的任务（从组内移除，不再被回收）。"""
        await self.connect()
        await self.redis.xack(
            self._stream_key, self._consumer_group, message_id
        )

    async def pending_count(self) -> int:
        await self.connect()
        try:
            info = await self.redis.xpending(
                self._stream_key, self._consumer_group
            )
            return int(info.get("pending", 0)) if isinstance(info, dict) else 0
        except aioredis.ResponseError:
            return 0

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


__all__ = ["LoopQueue"]