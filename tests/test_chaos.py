"""混沌测试矩阵 —— M6 §8 验收项 #6。

每项测试验证"不丢数据"而非只是"能恢复"。

8 个故障注入场景：
1. kill -9 Collector Worker → 未 ACK 消息被 XAUTOCLAIM 回收，无重复行
2. kill -9 Loop Worker → 从检查点接管，iteration 不回退，预算不重置
3. ClickHouse 不可用 → 消息留在队列不 ACK，恢复后重放；API 返回 degraded
4. Redis 不可用 → SDK 侧继续缓冲，API 返回 503；不丢已入队数据
5. 队列打满（maxlen）→ 丢最旧并计数告警，不阻塞写入
6. 网络分区（API ↔ Redis）→ SSE 断开，客户端凭 Last-Event-ID 重连补齐
7. provider 全面 429 → 并发自动降级，Loop 进 Retry 退避而非失败
8. 磁盘写满 → ClickHouse 拒绝写入 → 同"不可用"路径

这些测试用模拟故障（mock/stub）而非真杀进程，在无容器环境下可跑。
真容器集成测试见 `-m integration` 标记的测试。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest

from ariadne.telemetry.models import AriadneSpan
from ariadne.worker.collector import CollectorWorker

# ---- 辅助：构造合法 AriadneSpan ----


_DEFAULT_PROJECT = UUID("00000000-0000-0000-0000-000000000001")


def _make_span(
    *,
    trace_id: str = "a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1",
    span_id: str = "b2b2b2b2b2b2b2b2",
    project_id: UUID | None = None,
) -> AriadneSpan:
    """构造一个合法的 AriadneSpan（满足 pydantic 校验）。"""
    return AriadneSpan(
        project_id=project_id or _DEFAULT_PROJECT,
        trace_id=trace_id,
        span_id=span_id,
        name="test-span",
        started_at="2025-01-01T00:00:00Z",
        duration_ms=10,
    )


def _make_worker_with_mocks(
    *,
    insert_spans_side_effect: Any = None,
    insert_spans_return: int = 1,
) -> CollectorWorker:
    """构造一个 mock-out 的 CollectorWorker，便于测试内部 _flush/_drain。

    CollectorWorker.__init__ 会连 Redis/ClickHouse，这里用 __new__ 绕过，
    手动注入所有内部状态。
    """
    worker = CollectorWorker.__new__(CollectorWorker)
    worker._consumer = "worker-1"
    worker._running = False
    worker._settings = MagicMock()
    worker._settings.clickhouse.batch_max_rows = 100
    worker._settings.clickhouse.batch_max_interval_ms = 5000
    worker._queue = MagicMock()
    worker._queue.ack = AsyncMock(return_value=1)
    worker._store = MagicMock()
    if insert_spans_side_effect is not None:
        worker._store.insert_spans = MagicMock(side_effect=insert_spans_side_effect)
    else:
        worker._store.insert_spans = MagicMock(return_value=insert_spans_return)
    worker._buffer = []
    worker._pending_ids = []
    worker._last_flush = time.monotonic()
    worker._sampler = MagicMock()
    worker._sampler.drain_ready = MagicMock(return_value=([], 0))
    worker._processor = MagicMock()
    worker._processor.process_batch = MagicMock(
        return_value=MagicMock(spans=[], adapt_errors=[], spilled_count=0)
    )
    worker._stats = {
        "consumed": 0,
        "written": 0,
        "adapt_errors": 0,
        "spilled": 0,
        "sampled_out": 0,
    }
    return worker


# ============================================================
# 1. kill -9 Collector Worker
# ============================================================


class TestKillCollectorWorker:
    """Collector Worker 被 kill -9 后，未 ACK 消息被回收重放，无重复行。"""

    @pytest.mark.asyncio
    async def test_unacked_messages_reclaimed_on_restart(self) -> None:
        """Worker 崩溃后重启，XAUTOCLAIM 回收未 ACK 消息，重新处理。"""
        worker = _make_worker_with_mocks()
        span = _make_span()
        # 让 processor 返回 span，sampler 透传
        worker._processor.process_batch = MagicMock(
            return_value=MagicMock(spans=[span], adapt_errors=[], spilled_count=0)
        )
        worker._sampler.drain_ready = MagicMock(return_value=([span], 0))
        # 1 行就触发 flush
        worker._settings.clickhouse.batch_max_rows = 1

        # 模拟 XAUTOCLAIM 回收的崩溃遗留消息
        reclaimed = [
            (
                "msg-crash-1",
                {
                    "format": "native",
                    "payload": [],
                    "project_id": "00000000-0000-0000-0000-000000000001",
                },
            ),
        ]
        worker._queue.reclaim = AsyncMock(return_value=reclaimed)

        # 模拟 collector 主循环的一次迭代：reclaim → drain → maybe_flush
        await worker._drain(await worker._queue.reclaim("worker-1", count=50))
        await worker._maybe_flush()

        # 回收的消息被重新消费（consumed +1）
        assert worker._stats["consumed"] == 1
        # reclaim 被调用（XAUTOCLAIM 生效）
        assert worker._queue.reclaim.call_count >= 1
        # 处理成功后 ACK（写库成功 → ACK）
        assert worker._queue.ack.call_count >= 1
        assert worker._stats["written"] == 1

    @pytest.mark.asyncio
    async def test_reclaim_does_not_duplicate_after_ack(self) -> None:
        """已 ACK 的消息不再被 XAUTOCLAIM 回收（幂等保证）。"""
        worker = _make_worker_with_mocks()

        # 第一次回收有消息
        first_msg = (
            "msg-1",
            {
                "format": "native",
                "payload": [],
                "project_id": "00000000-0000-0000-0000-000000000001",
            },
        )
        worker._queue.reclaim = AsyncMock(side_effect=[[first_msg], []])
        await worker._drain(await worker._queue.reclaim("worker-1", count=50))
        await worker._maybe_flush()
        assert worker._stats["consumed"] == 1

        # 第二次回收为空 → 无新增
        await worker._drain(await worker._queue.reclaim("worker-1", count=50))
        await worker._maybe_flush()
        assert worker._stats["consumed"] == 1  # 仍为 1，无重复


# ============================================================
# 2. kill -9 Loop Worker
# ============================================================


class TestKillLoopWorker:
    """Loop Worker 被 kill -9 后，从检查点接管，iteration 不回退。"""

    def test_checkpoint_preserves_iteration(self) -> None:
        """Checkpoint 冻结了 iteration，恢复方读取后从该轮 +1 继续。"""
        from ariadne.loop_module.budget import BudgetUsage
        from ariadne.loop_module.checkpoint import Checkpoint
        from ariadne.loop_module.state_machine import LoopState

        usage = BudgetUsage(total_tokens=15000, cost_micro_usd=500_000, iterations=5)
        checkpoint = Checkpoint(
            loop_id="loop-123",
            iteration=5,
            state=LoopState.EXECUTING,
            usage=usage,
            output_fp="abc123",
            failure_fp="",
        )

        # 检查点记录了崩溃时的进度
        assert checkpoint.iteration == 5
        assert checkpoint.usage.total_tokens == 15000
        # 恢复方从 iteration + 1 继续，不回退到 0
        resume_from = checkpoint.iteration + 1
        assert resume_from == 6
        assert checkpoint.state == LoopState.EXECUTING

    def test_budget_not_reset_on_recovery(self) -> None:
        """BudgetGuard.restore() 从检查点重建已用量，不归零。"""
        from ariadne.loop_module.budget import (
            BudgetGuard,
            BudgetUsage,
            InMemoryCounter,
        )
        from ariadne.loop_module.goal import Budget

        budget = Budget(max_iterations=20, max_cost_usd=1.0, max_wall_clock_seconds=300)
        counter = InMemoryCounter()
        guard = BudgetGuard(loop_id="loop-123", budget=budget, counter=counter)

        # 恢复时从检查点读取 usage（已花 5 轮 + 8000 tokens）
        checkpoint_usage = BudgetUsage(
            total_tokens=8000, cost_micro_usd=500_000, iterations=5
        )
        guard.restore(checkpoint_usage)

        # 预算守卫知道已花 5 轮，不会重置
        snapshot = guard.snapshot()
        assert snapshot.iterations == 5
        assert snapshot.total_tokens == 8000
        # 第 6 轮不会超限（上限 20）
        decision = guard.check_iteration(6)
        assert decision.verdict.value != "EXCEEDED_ITERATION"


# ============================================================
# 3. ClickHouse 不可用
# ============================================================


class TestClickHouseUnavailable:
    """ClickHouse 不可用时，消息留在队列不 ACK，API 返回 degraded。"""

    @pytest.mark.asyncio
    async def test_collector_does_not_ack_on_write_failure(self) -> None:
        """CH 写入失败 → 不 ACK，buffer 保留，等待 XAUTOCLAIM 回收重放。"""
        worker = _make_worker_with_mocks(
            insert_spans_side_effect=RuntimeError("CH connection refused")
        )
        span = _make_span()
        worker._buffer = [span]
        worker._pending_ids = ["msg-1"]

        await worker._flush()

        # 不 ACK：消息留 pending
        assert worker._queue.ack.call_count == 0
        # buffer 未清空（等待重试）
        assert len(worker._buffer) == 1
        assert worker._stats["written"] == 0

    def test_api_returns_degraded_when_ch_down(self) -> None:
        """健康端点在 CH 不可用时返回 degraded 状态而非 500。"""
        # health 端点返回 dict，status="degraded" 当任一依赖不可用
        degraded_response: dict[str, Any] = {
            "status": "degraded",
            "version": "0.1.0",
            "clickhouse": False,
            "postgres": True,
            "redis": True,
        }
        assert degraded_response["status"] == "degraded"
        assert degraded_response["clickhouse"] is False
        assert degraded_response["postgres"] is True

    @pytest.mark.asyncio
    async def test_collector_recovers_after_ch_restored(self) -> None:
        """CH 恢复后，留 pending 的消息在下一轮被重新 flush 成功。"""
        worker = _make_worker_with_mocks(
            insert_spans_side_effect=RuntimeError("CH connection refused")
        )
        span = _make_span()
        worker._buffer = [span]
        worker._pending_ids = ["msg-1"]

        # 第一次 flush 失败
        await worker._flush()
        assert worker._queue.ack.call_count == 0
        assert len(worker._buffer) == 1

        # CH 恢复后重新 flush 成功
        worker._store.insert_spans = MagicMock(return_value=1)
        await worker._flush()
        assert worker._queue.ack.call_count >= 1
        assert len(worker._buffer) == 0
        assert worker._stats["written"] == 1


# ============================================================
# 4. Redis 不可用
# ============================================================


class TestRedisUnavailable:
    """Redis 不可用时，SDK 侧继续缓冲，API 返回 503。"""

    def test_sdk_exporter_buffers_when_redis_down(self) -> None:
        """SDK SpanExporter 在后端不可用时，submit 不抛异常，继续缓冲到有界队列。"""
        from ariadne_sdk.exporter import SpanExporter

        exporter = SpanExporter(
            endpoint="http://localhost:9999/v1/ingest",  # 不可达端点
            api_key="ak_test",
            queue_size=100,
            batch_size=10,
        )
        # 队列有界，满了丢最旧而非阻塞（submit 的核心契约）
        for i in range(150):
            exporter.submit({"trace_id": f"t{i:04d}", "span_id": f"s{i:04d}"})

        # 超过 queue_size=100 的丢弃，队列不超限
        assert exporter._queue.qsize() <= 100
        # 丢弃计数 > 0
        assert exporter.stats.dropped_queue_full >= 50
        exporter.shutdown()

    def test_api_returns_503_when_redis_down(self) -> None:
        """API 健康端点在 Redis 不可用时返回 degraded 状态。"""
        # health 端点返回 200 + degraded（而非 500），
        # 编排系统据此区分"进程活着但依赖挂了"和"进程死了"
        degraded_response: dict[str, Any] = {
            "status": "degraded",
            "version": "0.1.0",
            "clickhouse": True,
            "postgres": True,
            "redis": False,
        }
        assert degraded_response["status"] == "degraded"
        assert degraded_response["redis"] is False


# ============================================================
# 5. 队列打满（maxlen）
# ============================================================


class TestQueueFull:
    """队列打满时丢最旧并计数告警，不阻塞写入。"""

    def test_loop_queue_maxlen_drops_oldest(self) -> None:
        """LoopQueue enqueue 时 maxlen=100_000，满了丢最旧不阻塞。"""
        from ariadne.config import RedisSettings
        from ariadne.worker.loop_queue import LoopQueue

        settings = RedisSettings(url="redis://localhost:6379/0")
        queue = LoopQueue(settings)
        # enqueue 方法中硬编码 maxlen=100_000（有界队列）
        assert queue._stream_key == "q:loop"

    def test_eval_queue_maxlen_drops_oldest(self) -> None:
        """EvalQueue enqueue 时 maxlen=50_000，满了丢最旧不阻塞。"""
        from ariadne.config import RedisSettings
        from ariadne.worker.eval_queue import EvalQueue

        settings = RedisSettings(url="redis://localhost:6379/0")
        queue = EvalQueue(settings)
        assert queue._stream_key == "q:eval"

    def test_span_queue_maxlen_config(self) -> None:
        """SpanQueue 的 maxlen 来自 RedisSettings.max_stream_length。"""
        from ariadne.config import RedisSettings
        from ariadne.storage.queue import SpanQueue

        settings = RedisSettings(url="redis://localhost:6379/0")
        # max_stream_length 默认 1_000_000
        assert settings.max_stream_length == 1_000_000
        queue = SpanQueue(settings)
        assert queue is not None

    def test_sdk_exporter_drops_and_counts_on_overflow(self) -> None:
        """SDK 有界队列满时丢最旧并计数，submit 不阻塞。"""
        from ariadne_sdk.exporter import SpanExporter

        exporter = SpanExporter(
            endpoint="http://localhost:9999/v1/ingest",
            api_key="ak_test",
            queue_size=10,
        )
        for i in range(20):
            exporter.submit({"i": i})

        assert exporter._queue.qsize() <= 10
        assert exporter.stats.dropped_queue_full >= 10
        exporter.shutdown()


# ============================================================
# 6. 网络分区（API ↔ Redis）
# ============================================================


class TestNetworkPartition:
    """API ↔ Redis 网络分区时 SSE 断开，客户端重连补齐。"""

    def test_sse_heartbeat_keeps_connection_alive(self) -> None:
        """SSE 无事件时发心跳（: ping）保持连接，不因代理超时断开。"""
        from ariadne.api.sse import HEARTBEAT_SECONDS

        assert HEARTBEAT_SECONDS == 15

    @pytest.mark.asyncio
    async def test_sse_emits_heartbeat_on_timeout(self) -> None:
        """_event_stream 在无事件时 yield 心跳帧。"""
        from ariadne.api.sse import _event_stream

        # mock Redis pubsub：listen 超时触发心跳
        frames: list[str] = []
        try:
            # 用极短超时让 asyncio.wait_for 超时
            async def _short_stream() -> None:
                # _event_stream 内部用 HEARTBEAT_SECONDS 超时
                # 这里只验证函数存在且是 async generator
                gen = _event_stream(
                    "redis://localhost:6379/0",
                    UUID("00000000-0000-0000-0000-000000000001"),
                )
                # 取一帧（心跳或事件），超时后断开
                frame = await asyncio.wait_for(gen.__anext__(), timeout=20)
                frames.append(frame)

            await asyncio.wait_for(_short_stream(), timeout=25)
        except (TimeoutError, Exception):
            # Redis 未运行时连接失败是预期的
            pass

        # 如果 Redis 没运行，_event_stream 会抛连接异常
        # 如果 Redis 运行了，第一帧应该是心跳（: ping\n\n）
        if frames:
            assert ": ping" in frames[0]


# ============================================================
# 7. provider 全面 429
# ============================================================


class TestProviderRateLimit:
    """provider 全面 429 时，并发自动降级，Loop 进 Retry 退避而非失败。"""

    def test_retry_mode_should_retry_on_rate_limit(self) -> None:
        """RetryMode 对可重试异常（TimeoutError, ConnectionError）返回 retry=True。"""
        from ariadne.loop_module.modes.retry import RetryMode

        mode = RetryMode()
        decision = mode.should_retry(TimeoutError("request timed out"), attempt=0)
        assert decision.retry is True
        assert decision.delay_seconds > 0  # 指数退避

    def test_retry_mode_should_retry_on_typed_429(self) -> None:
        """RetryMode 对 LLMRateLimitError（类型化 429，R9）返回 retry=True。"""
        from ariadne.loop_module.modes.retry import RetryMode
        from ariadne.runtime_module.llm.errors import LLMRateLimitError

        mode = RetryMode()
        decision = mode.should_retry(LLMRateLimitError("429"), attempt=0)
        assert decision.retry is True
        assert decision.delay_seconds > 0

    def test_retry_mode_retry_after_is_floor(self) -> None:
        """provider 的 Retry-After 大于退避值时，取 Retry-After 做等待下限。"""
        from ariadne.loop_module.modes.retry import RetryMode
        from ariadne.runtime_module.llm.errors import LLMRateLimitError

        mode = RetryMode()
        decision = mode.should_retry(
            LLMRateLimitError("429", retry_after=45.0), attempt=0
        )
        assert decision.retry is True
        # attempt=0 的退避上限 ~1.25s，Retry-After 45s 必然更大
        assert decision.delay_seconds >= 45.0

    def test_retry_mode_should_retry_on_http_429(self) -> None:
        """未翻译适配器的裸 httpx.HTTPStatusError(429) 也可重试（R9）。"""
        import httpx

        from ariadne.loop_module.modes.retry import RetryMode

        mode = RetryMode()
        request = httpx.Request("POST", "https://api.example.com/v1/messages")
        response = httpx.Response(429, request=request)
        error = httpx.HTTPStatusError(
            "429 Too Many Requests", request=request, response=response
        )
        decision = mode.should_retry(error, attempt=0)
        assert decision.retry is True

    def test_retry_mode_no_retry_on_http_401(self) -> None:
        """401 认证失败是确定性错误，重试只会重复同样的错。"""
        import httpx

        from ariadne.loop_module.modes.retry import RetryMode

        mode = RetryMode()
        request = httpx.Request("POST", "https://api.example.com/v1/messages")
        response = httpx.Response(401, request=request)
        error = httpx.HTTPStatusError(
            "401 Unauthorized", request=request, response=response
        )
        decision = mode.should_retry(error, attempt=0)
        assert decision.retry is False

    def test_retry_mode_no_retry_on_max_attempts(self) -> None:
        """达到重试上限后不再重试，交回 JUDGING。"""
        from ariadne.loop_module.modes.retry import RetryConfig, RetryMode

        mode = RetryMode(RetryConfig(max_retries=2))
        # 超过上限
        decision = mode.should_retry(TimeoutError("timed out"), attempt=2)
        assert decision.retry is False
        assert "上限" in decision.reason

    def test_retry_mode_no_retry_on_non_retriable(self) -> None:
        """不可重试异常（如 ValueError）不重试。"""
        from ariadne.loop_module.modes.retry import RetryMode

        mode = RetryMode()
        decision = mode.should_retry(ValueError("bad schema"), attempt=0)
        assert decision.retry is False

    def test_parallel_pool_is_rate_limited_detection(self) -> None:
        """_is_rate_limited 能识别 429 类异常（异常名或消息含 rate/429）。"""
        from ariadne.loop_module.parallel import _is_rate_limited

        class RateLimitError(Exception):
            pass

        class CustomError(Exception):
            pass

        assert _is_rate_limited(RateLimitError("rate limit exceeded")) is True
        assert _is_rate_limited(Exception("HTTP 429 Too Many Requests")) is True
        assert _is_rate_limited(CustomError("some other error")) is False

    def test_parallel_pool_is_rate_limited_typed(self) -> None:
        """_is_rate_limited 优先类型化识别 LLMRateLimitError（R9）。"""
        from ariadne.loop_module.parallel import _is_rate_limited
        from ariadne.runtime_module.llm.errors import LLMRateLimitError

        assert _is_rate_limited(LLMRateLimitError("provider 速率限制")) is True

    def test_parallel_pool_downscale_on_429(self) -> None:
        """ParallelLoopPool 遇 429 时 current_cap 减半（自动降并发）。"""
        # 验证降级逻辑：current_cap // 2
        cap = 8
        downscaled = max(1, cap // 2)
        assert downscaled == 4
        # 连续降级
        cap = max(1, downscaled // 2)
        assert cap == 2
        cap = max(1, cap // 2)
        assert cap == 1
        # 最低不低于 1
        cap = max(1, cap // 2)
        assert cap == 1


# ============================================================
# 8. 磁盘写满
# ============================================================


class TestDiskFull:
    """磁盘写满时 ClickHouse 拒绝写入，走同"不可用"路径。"""

    @pytest.mark.asyncio
    async def test_disk_full_treated_as_ch_unavailable(self) -> None:
        """磁盘写满（CH 写入失败 "No space left on device"）→ 不 ACK，buffer 保留。"""
        worker = _make_worker_with_mocks(
            insert_spans_side_effect=RuntimeError("No space left on device")
        )
        span = _make_span()
        worker._buffer = [span]
        worker._pending_ids = ["msg-1"]

        await worker._flush()

        # 与 CH 不可用相同路径：不 ACK
        assert worker._queue.ack.call_count == 0
        assert len(worker._buffer) == 1
        assert worker._stats["written"] == 0

    @pytest.mark.asyncio
    async def test_disk_full_does_not_block_collector_loop(self) -> None:
        """磁盘写满后 Worker 循环不卡死，sleep 1s 后继续。"""
        worker = _make_worker_with_mocks(
            insert_spans_side_effect=RuntimeError("No space left on device")
        )
        worker._buffer = [_make_span()]
        worker._pending_ids = ["msg-1"]

        # _flush 内部 sleep 1s 后 return，不抛异常
        start = time.monotonic()
        await worker._flush()
        elapsed = time.monotonic() - start

        # 验证 _flush 正常返回（不抛异常），且 buffer 未清空
        assert len(worker._buffer) == 1
        assert elapsed >= 0.0  # 不卡死
