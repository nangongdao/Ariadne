"""Eval 任务队列 —— Redis Streams 消费者组封装。

与 LoopQueue 同构：API 创建 experiment 行后 XADD 入队（载荷是
experiment_id），EvalWorker 从消费者组认领 → 装配评测器 → 批量评测 →
回传结果 → ACK。崩溃时未 ACK 消息被 XAUTOCLAIM 回收。

三类 Worker 独立伸缩的依据：
  - collector-worker：高吞吐，CPU 轻量，按 span 速率伸缩
  - loop-worker：长任务，单任务可达分钟级，按并发 Loop 数伸缩
  - eval-worker：CPU + LLM 密集，批量评测可达分钟级，按实验并发伸缩
"""

from __future__ import annotations

from typing import Final

import redis.asyncio as aioredis

from ariadne.config import RedisSettings
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

_PAYLOAD_FIELD: Final = "experiment_id"
_PROJECT_FIELD: Final = "project_id"
_BUSYGROUP: Final = "BUSYGROUP"
RECLAIM_MIN_IDLE_MS: Final = 120_000  # 评测任务比 Loop 更长，放宽回收
_MAX_CLAIM: Final = 5


class EvalQueue:
    """Eval 任务队列。"""

    def __init__(
        self,
        settings: RedisSettings,
        *,
        stream_key: str = "q:eval",
        consumer_group: str = "eval-workers",
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
            raise RuntimeError("EvalQueue 未连接，请先 await connect()")
        return self._redis

    async def ping(self) -> bool:
        try:
            await self.connect()
            return bool(await self.redis.ping())
        except Exception as exc:
            logger.warning("redis ping failed", extra={"error": str(exc)})
            return False

    async def ensure_group(self) -> None:
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

    async def enqueue(
        self, experiment_id: str, project_id: str
    ) -> str:
        """提交一个待评测实验。"""
        await self.connect()
        message_id = await self.redis.xadd(
            self._stream_key,
            {_PAYLOAD_FIELD: experiment_id, _PROJECT_FIELD: project_id},
            maxlen=50_000,
            approximate=True,
        )
        return str(message_id)

    async def claim(
        self, consumer: str, *, count: int = _MAX_CLAIM, block_ms: int = 1000
    ) -> list[tuple[str, str, str]]:
        """认领任务，返回 [(message_id, experiment_id, project_id)]。"""
        await self.connect()
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
            pass
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
        seen: set[str] = set()
        unique: list[tuple[str, str, str]] = []
        for message_id, exp_id, proj_id in fresh:
            if exp_id and exp_id not in seen:
                seen.add(exp_id)
                unique.append((message_id, exp_id, proj_id))
        return unique

    async def ack(self, message_id: str) -> None:
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


__all__ = ["EvalQueue"]
