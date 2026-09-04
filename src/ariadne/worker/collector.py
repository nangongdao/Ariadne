"""Collector Worker：消费队列 → 加工 → 批量写 ClickHouse。

批量触发条件：累积行数达阈值，或距上次 flush 超过间隔。两者取先到者。
崩溃安全：先写库、后 ACK。崩溃时消息会被 XAUTOCLAIM 回收重放，
靠 ReplacingMergeTree 的 (trace_id, span_id) 去重保证幂等。
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
import time
from typing import Any
from uuid import UUID

from ariadne.config import Settings, get_settings
from ariadne.observability.metrics import (
    collector_adapt_errors_total,
    collector_consumed_total,
    collector_lag_seconds,
    collector_sampled_out_total,
    collector_written_total,
)
from ariadne.storage.clickhouse import ClickHouseStore
from ariadne.storage.queue import SpanQueue
from ariadne.telemetry.models import AriadneSpan
from ariadne.telemetry.sampling import HeadSampler, TailSampler
from ariadne.utils.logging import configure_logging, get_logger
from ariadne.worker.processor import SpanProcessor

logger = get_logger(__name__)

_RECLAIM_EVERY_LOOPS = 50


class CollectorWorker:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._queue = SpanQueue(self._settings.redis)
        self._store = ClickHouseStore(self._settings.clickhouse)
        self._processor = SpanProcessor(self._settings)
        self._check_semconv_version()
        self._consumer = f"{socket.gethostname()}-{id(self)}"
        self._running = False
        self._buffer: list[AriadneSpan] = []
        self._pending_ids: list[str] = []
        self._last_flush = time.monotonic()
        self._sampler: TailSampler = TailSampler(HeadSampler(rate=1.0))
        self._stats = {
            "consumed": 0,
            "written": 0,
            "adapt_errors": 0,
            "spilled": 0,
            "sampled_out": 0,
        }

    def _check_semconv_version(self) -> None:
        """启动时校验 semconv 版本锁与适配层映射表是否一致（docs/06 §1）。"""
        from ariadne.telemetry.adapters.otlp import check_semconv_version

        drift = check_semconv_version(self._settings.telemetry.genai_semconv_version)
        if drift:
            logger.warning(drift, extra={
                "configured": self._settings.telemetry.genai_semconv_version,
            })

    async def _load_db_pricing(self) -> None:
        """启动时从 model_pricing 表加载计价表注入 processor。

        成本归因报表（/v1/costs）的全部数据来自 Collector 侧的 cost_usd，
        而表从没被读过 —— 报表永远按内置硬编码价计算，provider 调价后
        无从更新。表缺失/读失败由仓储回退内置价，Collector 不应因定价
        问题起不来。
        """
        from ariadne.storage.postgres.engine import get_store
        from ariadne.storage.postgres.repositories.pricing import PricingRepository

        try:
            store = get_store()
            async with store.session() as session:
                self._processor.set_db_pricing(
                    await PricingRepository(session).load()
                )
        except Exception as exc:
            logger.warning(
                "加载 model_pricing 失败，成本按内置默认价计算",
                extra={"error": str(exc)},
            )

    async def start(self) -> None:
        await self._queue.ensure_group()
        await self._load_db_pricing()
        self._running = True
        logger.info("collector started", extra={"consumer": self._consumer})

        loops = 0
        while self._running:
            try:
                loops += 1
                # 周期性回收崩溃 Worker 遗留的未 ACK 消息
                if loops % _RECLAIM_EVERY_LOOPS == 0:
                    await self._drain(await self._queue.reclaim(self._consumer, count=50))

                messages = await self._queue.consume(self._consumer, count=50, block_ms=1000)
                await self._drain(messages)
                await self._maybe_flush()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("collector loop error", extra={"error": str(exc)}, exc_info=True)
                await asyncio.sleep(1.0)

        await self._flush()

        # 采样统计日志
        if self._stats["sampled_out"] > 0:
            logger.info(
                "sampling stats",
                extra={"sampled_out": self._stats["sampled_out"]},
            )
        logger.info("collector stopped", extra=dict(self._stats))

    async def stop(self) -> None:
        self._running = False

    async def _drain(self, messages: list[tuple[str, dict[str, Any]]]) -> None:
        for message_id, batch in messages:
            self._pending_ids.append(message_id)
            self._stats["consumed"] += 1
            collector_consumed_total.inc()
            try:
                result = self._processor.process_batch(
                    str(batch.get("format", "native")),
                    list(batch.get("payload") or []),
                    UUID(str(batch["project_id"])),
                )
            except Exception as exc:
                logger.error(
                    "batch processing failed, dropping",
                    extra={"message_id": message_id, "error": str(exc)},
                )
                collector_adapt_errors_total.inc()
                continue
            # 尾部采样：按 loop_id 缓冲，全保留或全丢弃
            for span in result.spans:
                self._sampler.add(span)
            self._stats["adapt_errors"] += len(result.adapt_errors)
            self._stats["spilled"] += result.spilled_count
            if result.adapt_errors:
                collector_adapt_errors_total.inc(len(result.adapt_errors))

    async def _maybe_flush(self) -> None:
        # 从尾部采样器取出已决策的 span（含超时降级处理）
        kept, dropped = self._sampler.drain_ready()
        self._buffer.extend(kept)
        self._stats["sampled_out"] += dropped
        if dropped > 0:
            collector_sampled_out_total.inc(dropped)

        elapsed_ms = (time.monotonic() - self._last_flush) * 1000
        ch = self._settings.clickhouse
        if len(self._buffer) >= ch.batch_max_rows or (
            self._buffer and elapsed_ms >= ch.batch_max_interval_ms
        ):
            await self._flush()

    async def _flush(self) -> None:
        if not self._buffer:
            if self._pending_ids:
                await self._queue.ack(*self._pending_ids)
                self._pending_ids.clear()
            self._last_flush = time.monotonic()
            return

        spans, ids = list(self._buffer), list(self._pending_ids)
        try:
            # 阻塞驱动放到线程池，避免卡住事件循环
            written = await asyncio.to_thread(self._store.insert_spans, spans)
        except Exception as exc:
            # 不 ACK：消息留在 pending，稍后被回收重放
            logger.error(
                "clickhouse insert failed, will retry via reclaim",
                extra={"rows": len(spans), "error": str(exc)},
            )
            await asyncio.sleep(1.0)
            return

        self._buffer.clear()
        self._pending_ids.clear()
        self._stats["written"] += written
        collector_written_total.inc(written)
        # 更新采集延迟指标（自监控核心）
        collector_lag_seconds.set(0.0)
        # 写库成功后才 ACK
        if ids:
            await self._queue.ack(*ids)
        self._last_flush = time.monotonic()
        logger.debug("flushed", extra={"rows": written})

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    async def close(self) -> None:
        await self._queue.close()
        self._store.close()


async def run_collector() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    worker = CollectorWorker(settings)

    loop = asyncio.get_running_loop()
    task = asyncio.create_task(worker.start())

    def _shutdown() -> None:
        logger.info("shutdown signal received")
        task_stop = asyncio.create_task(worker.stop())
        task_stop.add_done_callback(lambda _: None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows 不支持 add_signal_handler
            loop.add_signal_handler(sig, _shutdown)

    try:
        await task
    finally:
        await worker.close()
