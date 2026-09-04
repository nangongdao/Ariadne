"""Redis Streams 队列封装。

选 Streams 而非 List 的理由：需要消费者组 + ACK + 可见性超时。
Worker 崩溃时未 ACK 的消息由 XAUTOCLAIM 回收，不丢数据。
"""

from __future__ import annotations

import json
from typing import Any, Final

import redis.asyncio as aioredis

from ariadne.config import RedisSettings
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

_PAYLOAD_FIELD: Final = "data"
# BUSYGROUP 表示组已存在，是幂等创建的正常情况
_BUSYGROUP: Final = "BUSYGROUP"


class SpanQueue:
    """采集队列。生产者是 API，消费者是 Collector Worker。"""

    def __init__(self, settings: RedisSettings) -> None:
        self._settings = settings
        self._redis: aioredis.Redis | None = None

    async def connect(self) -> None:
        if self._redis is None:
            self._redis = aioredis.from_url(  # type: ignore[no-untyped-call]
                self._settings.url, encoding="utf-8", decode_responses=True
            )

    @property
    def redis(self) -> aioredis.Redis:
        if self._redis is None:
            raise RuntimeError("SpanQueue 未连接，请先 await connect()")
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
                self._settings.stream_key,
                self._settings.consumer_group,
                id="0",
                mkstream=True,
            )
            logger.info("consumer group created", extra={"group": self._settings.consumer_group})
        except aioredis.ResponseError as exc:
            if _BUSYGROUP not in str(exc):
                raise

    async def publish(self, batch: dict[str, Any]) -> str:
        """入队一个批次。maxlen 做有界裁剪，防止消费不及时打爆内存。"""
        await self.connect()
        message_id: str = await self.redis.xadd(
            self._settings.stream_key,
            {_PAYLOAD_FIELD: json.dumps(batch, ensure_ascii=False, default=str)},
            maxlen=self._settings.max_stream_length,
            approximate=True,
        )
        return message_id

    async def consume(
        self, consumer: str, *, count: int = 10, block_ms: int = 2000
    ) -> list[tuple[str, dict[str, Any]]]:
        """读取新消息。返回 [(message_id, batch)]。"""
        await self.connect()
        response = await self.redis.xreadgroup(
            self._settings.consumer_group,
            consumer,
            {self._settings.stream_key: ">"},
            count=count,
            block=block_ms,
        )
        return self._decode(response)

    async def reclaim(
        self, consumer: str, *, count: int = 10
    ) -> list[tuple[str, dict[str, Any]]]:
        """回收超过可见性超时仍未 ACK 的消息（原持有者崩溃）。"""
        await self.connect()
        _, messages, _ = await self.redis.xautoclaim(
            self._settings.stream_key,
            self._settings.consumer_group,
            consumer,
            min_idle_time=self._settings.visibility_timeout_ms,
            count=count,
        )
        decoded: list[tuple[str, dict[str, Any]]] = []
        for message_id, fields in messages:
            payload = self._parse(message_id, fields)
            if payload is not None:
                decoded.append((message_id, payload))
        return decoded

    async def ack(self, *message_ids: str) -> int:
        if not message_ids:
            return 0
        await self.connect()
        return int(
            await self.redis.xack(
                self._settings.stream_key, self._settings.consumer_group, *message_ids
            )
        )

    async def pending_count(self) -> int:
        await self.connect()
        try:
            info = await self.redis.xpending(
                self._settings.stream_key, self._settings.consumer_group
            )
            return int(info.get("pending", 0)) if isinstance(info, dict) else 0
        except aioredis.ResponseError:
            return 0

    async def stream_length(self) -> int:
        await self.connect()
        return int(await self.redis.xlen(self._settings.stream_key))

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    def _decode(self, response: Any) -> list[tuple[str, dict[str, Any]]]:
        decoded: list[tuple[str, dict[str, Any]]] = []
        for _stream, messages in response or []:
            for message_id, fields in messages:
                payload = self._parse(message_id, fields)
                if payload is not None:
                    decoded.append((message_id, payload))
        return decoded

    def _parse(self, message_id: str, fields: dict[str, str]) -> dict[str, Any] | None:
        """坏消息不能卡住队列：解析失败就记日志并让调用方 ACK 掉。"""
        raw = fields.get(_PAYLOAD_FIELD)
        if not raw:
            logger.warning("empty message", extra={"message_id": message_id})
            return None
        try:
            parsed: dict[str, Any] = json.loads(raw)
            return parsed
        except json.JSONDecodeError as exc:
            logger.error(
                "malformed message dropped",
                extra={"message_id": message_id, "error": str(exc)},
            )
            return None
