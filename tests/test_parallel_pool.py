"""并行 Loop 池测试。

验证 pipeline 语义、预算池共享、单项失败隔离、429 降并发。
全部用桩 LoopEngine（无需真实 LLM/Verifier/检查点）。
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from ariadne.loop_module.engine import LoopOutcome
from ariadne.loop_module.parallel import (
    BatchConfig,
    BatchItem,
    ItemState,
    ParallelLoopPool,
)
from ariadne.loop_module.rate_limit import AdaptiveRateLimitMonitor
from ariadne.loop_module.state_machine import LoopState
from ariadne.runtime_module.llm.errors import LLMRateLimitError


def _make_outcome(loop_id: str, converged: bool) -> LoopOutcome:
    return LoopOutcome(
        loop_id=loop_id,
        final_state=LoopState.CONVERGED if converged else LoopState.FAILED,
        iterations=3,
        usage=MagicMock(),
        verdict=None,
        converged=converged,
    )


class StubEngine:
    """按预置行为返回 outcome 的桩 engine。"""

    def __init__(
        self,
        outcome: LoopOutcome | None = None,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self._outcome = outcome
        self._error = error
        self._delay = delay
        self.run_count = 0

    async def run(self) -> LoopOutcome:
        self.run_count += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        if self._outcome is None:
            raise RuntimeError("no outcome configured")
        return self._outcome


class ScriptedEngineFactory:
    """按 loop_id 或顺序返回不同桩 engine 的工厂。"""

    def __init__(self, engines: list[StubEngine]) -> None:
        self._engines = list(engines)
        self._index = 0
        self.created: list[str] = []

    def create(self, config: Any) -> StubEngine:
        self.created.append(config.loop_id)
        if self._index < len(self._engines):
            engine = self._engines[self._index]
            self._index += 1
            return engine
        # 默认：返回第一个 engine（避免 IndexError）
        return self._engines[0]


def _make_item(loop_id: str, task: str = "test task") -> BatchItem:
    return BatchItem(loop_id=loop_id, task=task)


def _make_config_factory():
    """返回一个简单的 config_factory，用 BatchItem.loop_id 构造 LoopConfig。"""

    def factory(item: BatchItem) -> Any:
        config = MagicMock()
        config.loop_id = item.loop_id
        return config

    return factory


# ---------- 基础行为 ----------


@pytest.mark.asyncio
async def test_empty_batch():
    """空批次返回全零报告。"""
    pool = ParallelLoopPool(ScriptedEngineFactory([]))
    report = await pool.run_batch([], _make_config_factory())
    assert report.total == 0
    assert report.converged == 0
    assert report.results == ()


@pytest.mark.asyncio
async def test_single_item_converged():
    """单项批次：成功收敛。"""
    outcome = _make_outcome("loop-1", converged=True)
    factory = ScriptedEngineFactory([StubEngine(outcome=outcome)])
    pool = ParallelLoopPool(factory)
    report = await pool.run_batch([_make_item("loop-1")], _make_config_factory())
    assert report.total == 1
    assert report.converged == 1
    assert report.results[0].state is ItemState.CONVERGED
    assert report.results[0].outcome is outcome


@pytest.mark.asyncio
async def test_single_item_failed():
    """单项批次：失败（未收敛）。"""
    outcome = _make_outcome("loop-1", converged=False)
    factory = ScriptedEngineFactory([StubEngine(outcome=outcome)])
    pool = ParallelLoopPool(factory)
    report = await pool.run_batch([_make_item("loop-1")], _make_config_factory())
    assert report.failed == 1
    assert report.results[0].state is ItemState.FAILED


# ---------- pipeline 语义 ----------


@pytest.mark.asyncio
async def test_pipeline_not_barrier():
    """pipeline 语义：慢项不阻塞快项完成。

    构造 3 项：快(0.01s)、慢(0.3s)、快(0.01s)。
    并发上限 3 时，两个快项应先完成，不必等慢项。
    用完成时间戳验证：快项的完成时间应明显早于慢项。
    """
    fast_outcome = _make_outcome("fast", True)
    slow_outcome = _make_outcome("slow", True)
    fast2_outcome = _make_outcome("fast2", True)

    completion_times: dict[str, float] = {}

    class TimedEngine(StubEngine):
        async def run(self) -> LoopOutcome:
            import time

            result = await super().run()
            completion_times[self._outcome.loop_id] = time.monotonic()
            return result

    factory = ScriptedEngineFactory([
        TimedEngine(outcome=fast_outcome, delay=0.01),
        TimedEngine(outcome=slow_outcome, delay=0.3),
        TimedEngine(outcome=fast2_outcome, delay=0.01),
    ])
    pool = ParallelLoopPool(factory)
    report = await pool.run_batch(
        [_make_item("fast"), _make_item("slow"), _make_item("fast2")],
        _make_config_factory(),
        BatchConfig(max_concurrency=3),
    )
    assert report.converged == 3
    # 快项应比慢项先完成
    assert completion_times["fast"] < completion_times["slow"]
    assert completion_times["fast2"] < completion_times["slow"]


@pytest.mark.asyncio
async def test_concurrency_limit_respected():
    """并发上限被遵守：max_concurrency=2 时最多 2 项同时跑。"""
    current = 0
    peak = 0
    lock = asyncio.Lock()

    class CountingEngine(StubEngine):
        async def run(self) -> LoopOutcome:
            nonlocal current, peak
            async with lock:
                current += 1
                if current > peak:
                    peak = current
            await asyncio.sleep(0.05)
            async with lock:
                current -= 1
            return await super().run()

    outcomes = [_make_outcome(f"loop-{i}", True) for i in range(6)]
    engines = [CountingEngine(outcome=o) for o in outcomes]
    factory = ScriptedEngineFactory(engines)
    pool = ParallelLoopPool(factory)
    report = await pool.run_batch(
        [_make_item(f"loop-{i}") for i in range(6)],
        _make_config_factory(),
        BatchConfig(max_concurrency=2),
    )
    assert report.converged == 6
    assert peak <= 2, f"peak concurrency {peak} exceeded limit 2"


# ---------- 单项失败隔离 ----------


@pytest.mark.asyncio
async def test_item_failure_doesnt_affect_others():
    """一项失败不影响其他项：失败项报错，其余仍 CONVERGED。"""
    ok_outcome = _make_outcome("ok", True)
    fail_outcome = _make_outcome("fail", False)
    ok2_outcome = _make_outcome("ok2", True)

    factory = ScriptedEngineFactory([
        StubEngine(outcome=ok_outcome),
        StubEngine(outcome=fail_outcome),
        StubEngine(outcome=ok2_outcome),
    ])
    pool = ParallelLoopPool(factory)
    report = await pool.run_batch(
        [_make_item("ok"), _make_item("fail"), _make_item("ok2")],
        _make_config_factory(),
    )
    assert report.converged == 2
    assert report.failed == 1
    assert report.results[0].state is ItemState.CONVERGED
    assert report.results[1].state is ItemState.FAILED
    assert report.results[2].state is ItemState.CONVERGED


@pytest.mark.asyncio
async def test_engine_exception_isolated():
    """engine 抛异常时该项标记 FAILED，不影响其他项。"""
    ok_outcome = _make_outcome("ok", True)
    factory = ScriptedEngineFactory([
        StubEngine(outcome=ok_outcome),
        StubEngine(error=RuntimeError("engine crashed")),
        StubEngine(outcome=ok_outcome),
    ])
    pool = ParallelLoopPool(factory)
    report = await pool.run_batch(
        [_make_item("ok"), _make_item("crash"), _make_item("ok2")],
        _make_config_factory(),
    )
    assert report.converged == 2
    assert report.failed == 1
    assert "RuntimeError" in report.results[1].error


# ---------- 429 降并发 ----------


@pytest.mark.asyncio
async def test_rate_limit_retry_and_downscale():
    """429 时自动重试，重试成功后收敛。"""
    ok_outcome = _make_outcome("retry-ok", True)

    class RetryThenSucceed(StubEngine):
        def __init__(self) -> None:
            super().__init__()
            self._calls = 0

        async def run(self) -> LoopOutcome:
            self._calls += 1
            if self._calls == 1:
                raise RuntimeError("Rate limit exceeded: 429")
            return ok_outcome

    factory = ScriptedEngineFactory([RetryThenSucceed()])
    pool = ParallelLoopPool(factory)
    report = await pool.run_batch(
        [_make_item("retry-ok")],
        _make_config_factory(),
        BatchConfig(max_retries=3),
    )
    assert report.converged == 1
    assert report.results[0].state is ItemState.CONVERGED


@pytest.mark.asyncio
async def test_rate_limit_exhausted_retries():
    """429 重试耗尽后标记 FAILED。"""

    class AlwaysRateLimited(StubEngine):
        async def run(self) -> LoopOutcome:
            raise RuntimeError("429 Too Many Requests")

    factory = ScriptedEngineFactory([AlwaysRateLimited()])
    pool = ParallelLoopPool(factory)
    report = await pool.run_batch(
        [_make_item("always-429")],
        _make_config_factory(),
        BatchConfig(max_retries=2),
    )
    assert report.failed == 1
    assert "429" in report.results[0].error or "max retries" in report.results[0].error


@pytest.mark.asyncio
async def test_typed_rate_limit_error_retried():
    """LLMRateLimitError（类型化 429）走重试路径，Retry-After 被喂入监控器。"""
    ok_outcome = _make_outcome("typed-ok", True)

    class TypedRetryThenSucceed(StubEngine):
        def __init__(self) -> None:
            super().__init__()
            self._calls = 0

        async def run(self) -> LoopOutcome:
            self._calls += 1
            if self._calls == 1:
                raise LLMRateLimitError("provider 速率限制", retry_after=0.5)
            return ok_outcome

    class RecordingMonitor:
        def __init__(self) -> None:
            self.limited: list[float | None] = []
            self.successes = 0

        def available_concurrency(self, cap: int) -> int:
            return cap

        def note_rate_limited(self, retry_after: float | None = None) -> None:
            self.limited.append(retry_after)

        def note_success(self) -> None:
            self.successes += 1

    monitor = RecordingMonitor()
    factory = ScriptedEngineFactory([TypedRetryThenSucceed()])
    pool = ParallelLoopPool(factory, rate_monitor=monitor)
    report = await pool.run_batch(
        [_make_item("typed-ok")],
        _make_config_factory(),
        BatchConfig(max_retries=3),
    )
    assert report.converged == 1
    # Retry-After 从异常结构化传到监控器
    assert monitor.limited == [0.5]
    # 成功喂入监控器
    assert monitor.successes == 1


@pytest.mark.asyncio
async def test_adaptive_monitor_reduces_batch_concurrency():
    """429 后余量折减：下一个批次的动态并发上限随之下降。"""
    monitor = AdaptiveRateLimitMonitor()
    ok_outcome = _make_outcome("ok", True)
    factory = ScriptedEngineFactory([
        StubEngine(outcome=ok_outcome) for _ in range(8)
    ])
    pool = ParallelLoopPool(factory, rate_monitor=monitor)

    # 第一批：满额并发跑完
    first = await pool.run_batch(
        [_make_item(f"a-{i}") for i in range(8)],
        _make_config_factory(),
        BatchConfig(max_concurrency=8),
    )
    assert first.converged == 8
    assert monitor.factor == 1.0

    # 模拟一批 429 全员撞墙（直接喂监控器——等价于池内 429 的效果）
    monitor.note_rate_limited(None)
    assert monitor.factor == 0.5
    # available_concurrency 在冷却期为 0：下一批次串行化
    assert monitor.available_concurrency(8) == 0


# ---------- 取消 ----------


@pytest.mark.asyncio
async def test_batch_cancel_skips_items():
    """批次取消后，未开始的项被标记 SKIPPED。"""
    cancel_event = asyncio.Event()
    cancel_event.set()  # 立即取消

    ok_outcome = _make_outcome("ok", True)
    factory = ScriptedEngineFactory([StubEngine(outcome=ok_outcome)])
    pool = ParallelLoopPool(factory)
    report = await pool.run_batch(
        [_make_item("a"), _make_item("b")],
        _make_config_factory(),
        BatchConfig(cancel_event=cancel_event),
    )
    assert report.skipped == 2
    assert all(r.state is ItemState.SKIPPED for r in report.results)


# ---------- 报告完整性 ----------


@pytest.mark.asyncio
async def test_batch_report_fields():
    """BatchReport 的各字段正确统计。"""
    outcomes = [
        _make_outcome("loop-0", True),
        _make_outcome("loop-1", False),
        _make_outcome("loop-2", True),
        _make_outcome("loop-3", False),
    ]
    factory = ScriptedEngineFactory([StubEngine(outcome=o) for o in outcomes])
    pool = ParallelLoopPool(factory)
    report = await pool.run_batch(
        [_make_item(f"loop-{i}") for i in range(4)],
        _make_config_factory(),
    )
    assert report.total == 4
    assert report.converged == 2
    assert report.failed == 2
    assert report.skipped == 0
    assert len(report.results) == 4
    assert report.success_rate == 0.5
    # results 按 item_index 排序
    assert report.results[0].item_index == 0
    assert report.results[3].item_index == 3
    assert report.peak_concurrency >= 1


# ---------- 动态并发 ----------


@pytest.mark.asyncio
async def test_dynamic_concurrency_rate_monitor():
    """rate_monitor 限制初始并发上限。"""
    ok_outcome = _make_outcome("ok", True)
    factory = ScriptedEngineFactory([
        StubEngine(outcome=ok_outcome) for _ in range(4)
    ])

    class LowRateMonitor:
        def available_concurrency(self, cap: int) -> int:
            return 1

        def note_rate_limited(self, retry_after: float | None = None) -> None:
            return None

        def note_success(self) -> None:
            return None

    pool = ParallelLoopPool(factory, rate_monitor=LowRateMonitor())
    report = await pool.run_batch(
        [_make_item(f"loop-{i}") for i in range(4)],
        _make_config_factory(),
        BatchConfig(max_concurrency=10),
    )
    assert report.converged == 4
    # 初始 cap = min(10, 1, 10000) = 1
    assert report.peak_concurrency <= 1


@pytest.mark.asyncio
async def test_dynamic_concurrency_sandbox_monitor():
    """sandbox_monitor 限制初始并发上限。"""
    ok_outcome = _make_outcome("ok", True)
    factory = ScriptedEngineFactory([
        StubEngine(outcome=ok_outcome) for _ in range(4)
    ])

    class LowSandboxMonitor:
        def available_slots(self) -> int:
            return 2

    pool = ParallelLoopPool(factory, sandbox_monitor=LowSandboxMonitor())
    report = await pool.run_batch(
        [_make_item(f"loop-{i}") for i in range(4)],
        _make_config_factory(),
        BatchConfig(max_concurrency=10),
    )
    assert report.converged == 4
    # 初始 cap = min(10, 10000, 2) = 2
    assert report.peak_concurrency <= 2


@pytest.mark.asyncio
async def test_config_factory_called_per_item():
    """config_factory 对每项独立调用，产出独立 LoopConfig。"""
    ok_outcome = _make_outcome("ok", True)
    factory = ScriptedEngineFactory([
        StubEngine(outcome=ok_outcome) for _ in range(3)
    ])
    pool = ParallelLoopPool(factory)

    created_ids: list[str] = []

    def tracking_factory(item: BatchItem) -> Any:
        config = MagicMock()
        config.loop_id = item.loop_id
        created_ids.append(item.loop_id)
        return config

    await pool.run_batch(
        [_make_item("a"), _make_item("b"), _make_item("c")],
        tracking_factory,
    )
    assert sorted(created_ids) == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_engine_factory_called_per_item():
    """engine_factory 对每项独立调用，每项有独立 engine 实例。"""
    ok_outcome = _make_outcome("ok", True)
    engines = [StubEngine(outcome=ok_outcome) for _ in range(3)]
    factory = ScriptedEngineFactory(engines)
    pool = ParallelLoopPool(factory)

    await pool.run_batch(
        [_make_item("a"), _make_item("b"), _make_item("c")],
        _make_config_factory(),
    )
    assert len(factory.created) == 3
    # 每个 engine 的 run_count 应为 1
    for engine in engines:
        assert engine.run_count == 1


# ---------- ConcurrencyGate：429 降并发必须真的收缩闸门 ----------


class TestConcurrencyGate:
    """Semaphore 容量固定，收缩只能靠自建闸门。

    原实现的"降并发"只把一个局部变量减半再写进日志，闸门容量从未变过。
    这些测试直接断言闸门行为，证明 downscale 会真的拦住后续任务。
    """

    async def test_downscale_blocks_new_acquisitions(self):
        from ariadne.loop_module.parallel import ConcurrencyGate

        gate = ConcurrencyGate(4)
        for _ in range(4):
            await gate.acquire()
        assert gate.in_flight == 4

        assert await gate.downscale() == 2

        # 释放两个后 in_flight == 2 == 新容量：第五个任务仍必须等
        await gate.release()
        await gate.release()
        acquired = asyncio.Event()

        async def waiter() -> None:
            await gate.acquire()
            acquired.set()

        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)
        assert not acquired.is_set(), "降并发后新任务不该立即通过"

        # 再释放一个，in_flight == 1 < 2，等待者才能进入
        await gate.release()
        await asyncio.wait_for(task, timeout=1.0)
        assert acquired.is_set()

    async def test_downscale_floor_is_one(self):
        from ariadne.loop_module.parallel import ConcurrencyGate

        gate = ConcurrencyGate(2)
        assert await gate.downscale() == 1
        assert await gate.downscale() == 1  # 不能再降

    async def test_restore_recovers_but_not_beyond_ceiling(self):
        from ariadne.loop_module.parallel import ConcurrencyGate

        gate = ConcurrencyGate(4)
        await gate.downscale()  # 2
        assert await gate.restore() == 3
        assert await gate.restore() == 4
        assert await gate.restore() == 4  # 封顶

    async def test_slot_releases_on_exception(self):
        from ariadne.loop_module.parallel import ConcurrencyGate

        gate = ConcurrencyGate(1)
        with pytest.raises(RuntimeError):
            async with gate.slot():
                raise RuntimeError("boom")
        assert gate.in_flight == 0


@pytest.mark.asyncio
async def test_rate_limit_downscale_actually_blocks_new_dispatch():
    """429 退避期间不得再放新任务进闸门 —— 这是新旧实现唯一的行为差异。

    时间线：item-0 先睡 0.01s 再抛 429。这个前置睡眠不可省——engine 若在
    事件循环的第一个切片里就同步抛 429，其余项还没来得及进场，闸门收缩成 1
    后批次整体串行，断言测到的会是"槽位没发出去"而非"退避拦住了新任务"。
    前置睡眠让前三个槽位真实填满：item-0 + 两个记录器在 t≈0 进场，429 后
    闸门 3→1，item-0 在槽位内退避 ~1s 再重试。两个记录器 t≈0.2s 跑完释放
    槽位：旧实现里剩余项立刻按原并发进场（进场时刻 ≈0.2s）；新实现里
    capacity=1 且唯一的槽位被退避中的 item-0 占着，新任务要等重试成功、
    容量回升后才进得来（进场时刻 ≥ 退避下限 0.75s）。阈值取 0.5s，两边
    各有余量。用记录器的**进入时刻**断言，不依赖重试后的回升节奏。
    """
    ok = _make_outcome("later", True)
    starts: list[float] = []
    loop = asyncio.get_event_loop()

    class ConcurrencyRecorder(StubEngine):
        """记录 run() 的进入时刻。"""

        async def run(self) -> LoopOutcome:
            starts.append(loop.time())
            await asyncio.sleep(0.2)
            return ok

    class RateLimitOnce(StubEngine):
        """先睡 0.01s 再 429，重试成功 —— 触发 downscale + 退避。"""

        def __init__(self) -> None:
            super().__init__()
            self._calls = 0

        async def run(self) -> LoopOutcome:
            self._calls += 1
            if self._calls == 1:
                await asyncio.sleep(0.01)
                raise RuntimeError("429 rate limited")
            return ok

    factory = ScriptedEngineFactory(
        [RateLimitOnce()] + [ConcurrencyRecorder() for _ in range(5)]
    )
    pool = ParallelLoopPool(factory)
    report = await pool.run_batch(
        [_make_item(f"i{n}") for n in range(6)],
        _make_config_factory(),
        BatchConfig(max_concurrency=3, max_retries=3),
    )
    assert report.converged == 6

    # 前 3 个槽位被 item-0 + 2 个记录器占住（记录器在 t≈0 进场）。
    # 429 后闸门 3→1，唯一的槽位被退避中的 item-0 占用；退避下限
    # 0.75s（RETRY_BASE_DELAY * 0.75）。旧实现里剩余 3 项会在头两个
    # 记录器完成时（t≈0.2s）进场；新实现里它们必须等重试成功、容量
    # 回升后才能进。阈值取 0.5s，两边各有余量。
    assert len(starts) == 5
    late = [t - starts[0] for t in starts[2:]]
    assert min(late) > 0.5, (
        f"429 退避期间仍有任务进场（最早 {min(late):.2f}s），"
        "降并发没有真正收缩闸门"
    )
