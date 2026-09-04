"""并行 Loop 池 —— pipeline 语义批量执行。

批量场景（生成 N 篇文档、修 N 个测试）的执行策略。

核心设计决策（docs/M5-spec §3.2）：

| 语义 | 墙钟时间 | 评价 |
|---|---|---|
| barrier（每阶段等齐） | 每阶段最慢项之和 | 慢，单项卡住拖累全部 |
| **pipeline（各项独立跑完）** | 最慢单项 | 选定 |

共享：全局预算池（ProjectPoolCounter 原子计数）、并发上限、
provider 速率限制令牌。
隔离：每项独立的 loop_id、检查点、沙箱实例、失败指纹。

并发度 = `min(配置上限, provider 速率余量, 沙箱池容量)`，
动态计算避免把上游打到 429。429 时自动降并发并重试。

关键：单项失败不影响其他项 —— 用 asyncio.gather(return_exceptions=True)
+ 每项独立 LoopEngine 实例实现隔离。不是 barrier 语义：不等待"同批
都完成再进下一批"，而是用一个固定大小的并发槽位（Semaphore），
一项完成立即释放槽位让下一项进入。

所有外部依赖（LoopEngine 工厂、预算池、速率计、沙箱池、事件 sink）
抽象成 Protocol，单元测试用纯桩即可验证全部路径。
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from ariadne.loop_module.engine import LoopConfig, LoopOutcome
from ariadne.runtime_module.llm.errors import LLMRateLimitError
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

# 429 检测的退避基数（秒）。指数退避 + 抖动：base * 2^attempt，上限 30s。
# 抖动 ±25%——多 Worker 同步重试会打出一波同步 429，抖动打散重试时刻。
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 30.0
RETRY_MAX_ATTEMPTS = 3
RETRY_JITTER = 0.25


class ItemState(StrEnum):
    """批次中单项的执行状态。"""

    PENDING = "pending"
    RUNNING = "running"
    CONVERGED = "converged"
    FAILED = "failed"
    SKIPPED = "skipped"  # 预算池耗尽或批次取消


@dataclass(frozen=True)
class BatchItem:
    """批次中的一个执行项。

    每项有独立的 loop_id（隔离检查点与预算计数）、独立的 goal/inputs。
    inputs 是注入到 Loop 上下文的初始输入（如 RAG 检索结果、测试文件内容）。
    """

    loop_id: str
    task: str
    inputs: dict[str, Any] = field(default_factory=dict)
    # 项级覆盖：允许单项用不同模型/预算，None 时用批次默认值
    model_override: str | None = None


@dataclass(frozen=True)
class BatchResult:
    """单项的最终结果。"""

    loop_id: str
    item_index: int
    state: ItemState
    outcome: LoopOutcome | None
    error: str = ""

    @property
    def converged(self) -> bool:
        return self.state is ItemState.CONVERGED


@dataclass(frozen=True)
class BatchReport:
    """批次执行报告。"""

    batch_id: str
    total: int
    converged: int
    failed: int
    skipped: int
    results: tuple[BatchResult, ...]
    # 实际使用的最大并发度（用于调优和观测）
    peak_concurrency: int

    @property
    def success_rate(self) -> float:
        return self.converged / self.total if self.total else 0.0


@dataclass(frozen=True)
class BatchConfig:
    """批次执行配置。"""

    max_concurrency: int = 4
    # 预算池上限（微美分）。None 时不做项目级预算检查（仅单项预算）。
    pool_cost_limit_micro: int | None = None
    # 429 退避重试上限
    max_retries: int = RETRY_MAX_ATTEMPTS
    # 取消信号：外部设置后批次尽快停止派发新项
    cancel_event: asyncio.Event | None = None


class LoopEngineFactory(Protocol):
    """按批次项构造 LoopEngine 的工厂。

    抽象成 Protocol 让测试用桩 engine（直接返回预置 outcome），
    生产用真实 LoopEngine（组装 LLM/Verifier/检查点等）。
    """

    def create(self, config: LoopConfig) -> Any: ...


class RateLimitMonitor(Protocol):
    """provider 速率余量探针。

    available_concurrency 返回在给定配置上限 cap 下当前允许的并发数：
    实现可按观察到的 429 信号折减（甚至返回 0 冷却暂停），池的动态
    并发随之下降。note_* 由池喂入信号（429 / 成功），实现据此调整余量。
    生产实现可查 provider 的 rate-limit headers 或本地滑动窗口计数
    （AdaptiveRateLimitMonitor 是进程内参考实现）。
    """

    def available_concurrency(self, cap: int) -> int: ...

    def note_rate_limited(self, retry_after: float | None = None) -> None: ...

    def note_success(self) -> None: ...


class SandboxPoolMonitor(Protocol):
    """沙箱池余量探针。

    返回当前可用的沙箱实例数。并行 Loop 每项需要独立沙箱，
    池子空了就降并发而非排队等（排队会退化成 barrier）。
    """

    def available_slots(self) -> int: ...


class DefaultRateLimitMonitor:
    """不限制。仅用于测试或无 provider 限制的场景。"""

    def available_concurrency(self, cap: int) -> int:
        return cap  # 原样放行，min() 会取其他维度

    def note_rate_limited(self, retry_after: float | None = None) -> None:
        return None

    def note_success(self) -> None:
        return None


class DefaultSandboxPoolMonitor:
    """不限制。仅用于测试或无沙箱场景。"""

    def available_slots(self) -> int:
        return 10_000


def _is_rate_limited(error: Exception) -> bool:
    """判断异常是否为 provider 429。

    优先类型化判断（LLMRateLimitError，R9 起由适配器翻译 429 产生）；
    字符串匹配兜底其他形态的 429 异常（如桩引擎直接抛的 RuntimeError）。
    """
    if isinstance(error, LLMRateLimitError):
        return True
    name = type(error).__name__.lower()
    msg = str(error).lower()
    return "rate" in name or "429" in msg or "rate limit" in msg


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """429 重试等待：指数退避 + ±25% 抖动，provider 给 Retry-After 时取较大值。"""
    delay = min(RETRY_BASE_DELAY * (2**attempt), RETRY_MAX_DELAY)
    delay = random.uniform(delay * (1 - RETRY_JITTER), delay * (1 + RETRY_JITTER))
    if retry_after is not None and retry_after > 0:
        delay = max(retry_after, delay)
    return delay


class ConcurrencyGate:
    """容量可变的并发闸门。

    `asyncio.Semaphore` 的容量在构造时固定，没有公开的收缩方式 —— 原先
    "429 降并发"只是把一个局部变量减半再写进日志，闸门容量从头到尾没动过：
    读日志的人以为并发降了，上游继续被同样的并发打到 429。

    这里用"已发放许可数 vs 目标容量"自己算，于是 `downscale()` 立即生效：
    收缩后不再放行新任务，直到在途数落到新容量以下。已经在跑的任务不会
    被中断（中途掐断 Loop 只会浪费已花的 token）。

    `restore()` 用于连续成功后逐步回升 —— 只降不升会让一次偶发 429 永久
    压低吞吐。
    """

    def __init__(self, capacity: int, *, floor: int = 1) -> None:
        self._floor = max(1, floor)
        self._ceiling = max(self._floor, capacity)
        self._capacity = self._ceiling
        self._in_flight = 0
        self._condition = asyncio.Condition()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def acquire(self) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._in_flight < self._capacity)
            self._in_flight += 1

    async def release(self) -> None:
        async with self._condition:
            self._in_flight = max(0, self._in_flight - 1)
            self._condition.notify_all()

    async def downscale(self, *, factor: int = 2) -> int:
        """并发减半（不低于 floor）。返回新容量。"""
        async with self._condition:
            self._capacity = max(self._floor, self._capacity // max(2, factor))
            return self._capacity

    async def restore(self, *, step: int = 1) -> int:
        """并发回升一档（不超过初始上限）。返回新容量。"""
        async with self._condition:
            if self._capacity < self._ceiling:
                self._capacity = min(self._ceiling, self._capacity + max(1, step))
                self._condition.notify_all()
            return self._capacity

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        await self.acquire()
        try:
            yield
        finally:
            await self.release()


class ParallelLoopPool:
    """并行 Loop 执行池。

    pipeline 语义：用 ConcurrencyGate（容量可变的闸门）控制并发上限，
    每项独立跑完。不是 barrier：不分组等待，一项完成立即释放槽位让下一项进入。

    动态并发 = min(配置上限, provider 速率余量, 沙箱池容量)。
    429 时闸门容量真的收缩（不是只改日志），等连续成功后再逐步回升。
    """

    def __init__(
        self,
        engine_factory: LoopEngineFactory,
        *,
        rate_monitor: RateLimitMonitor | None = None,
        sandbox_monitor: SandboxPoolMonitor | None = None,
    ) -> None:
        self._engine_factory = engine_factory
        self._rate_monitor = rate_monitor or DefaultRateLimitMonitor()
        self._sandbox_monitor = sandbox_monitor or DefaultSandboxPoolMonitor()

    async def run_batch(
        self,
        items: list[BatchItem],
        config_factory: Callable[[BatchItem], LoopConfig],
        batch_config: BatchConfig | None = None,
    ) -> BatchReport:
        """执行一个批次。

        config_factory 按每项的 BatchItem 构造独立的 LoopConfig
        （含独立 loop_id、预算 guard、检查点 store）。

        返回 BatchReport，每项独立的结果在 results 里按 item_index 对应。
        """
        cfg = batch_config or BatchConfig()
        batch_id = f"batch-{id(items)}"  # 简单唯一 ID，生产可换 UUID
        total = len(items)

        if total == 0:
            return BatchReport(
                batch_id=batch_id,
                total=0,
                converged=0,
                failed=0,
                skipped=0,
                results=(),
                peak_concurrency=0,
            )

        # 动态并发上限的初始值：配置上限与 provider 余量、沙箱容量取 min
        initial_cap = min(
            cfg.max_concurrency,
            self._rate_monitor.available_concurrency(cfg.max_concurrency),
            self._sandbox_monitor.available_slots(),
        )
        gate = ConcurrencyGate(max(1, initial_cap))
        peak_concurrency = 0

        def _track_peak() -> None:
            nonlocal peak_concurrency
            peak_concurrency = max(peak_concurrency, gate.in_flight)

        results: list[BatchResult | None] = [None] * total

        async def _run_item(
            item: BatchItem,
            index: int,
        ) -> BatchResult:
            cancel = cfg.cancel_event
            if cancel is not None and cancel.is_set():
                return BatchResult(
                    loop_id=item.loop_id,
                    item_index=index,
                    state=ItemState.SKIPPED,
                    outcome=None,
                    error="batch cancelled",
                )

            async with gate.slot():
                _track_peak()

                loop_config = config_factory(item)
                engine = self._engine_factory.create(loop_config)

                attempt = 0
                while attempt < cfg.max_retries:
                    try:
                        outcome = await engine.run()

                        # 主动配额监控：从 outcome 中提取最后一次 LLM 调用的配额信息
                        # 预测配额耗尽前主动降并发（双重防线：配额预警 + 429 退避）
                        last_quota = getattr(outcome, "last_rate_limit_quota", None)
                        if last_quota is not None:
                            from ariadne.loop_module.rate_limit import quota_based_concurrency

                            suggested_cap = quota_based_concurrency(
                                last_quota, cfg.max_concurrency
                            )
                            current_cap = gate.capacity

                            # 配额接近耗尽：主动降并发（无需等 429）
                            if suggested_cap < current_cap:
                                # downscale 会减半，这里直接设置目标容量更精确
                                # 但 ConcurrencyGate 只提供 downscale（减半）和 restore（+1）
                                # 暂时用 downscale 实现，后续可扩展 set_capacity
                                while gate.capacity > suggested_cap:
                                    await gate.downscale(factor=2)
                                logger.info(
                                    "proactive concurrency reduction based on quota",
                                    extra={
                                        "loop_id": item.loop_id,
                                        "requests_remaining": getattr(
                                            last_quota, "requests_remaining", None
                                        ),
                                        "tokens_remaining": getattr(
                                            last_quota, "tokens_remaining", None
                                        ),
                                        "old_cap": current_cap,
                                        "new_cap": gate.capacity,
                                    },
                                )

                        self._rate_monitor.note_success()
                        if attempt > 0:
                            # 本项经历过 429、退避后重试成功：证明 provider
                            # 恢复了，回升一档并发。未经过 429 的项即使成功
                            # 也不回升 —— 它们起跑早于限流，其成功不构成
                            # 恢复证据，反而会把刚收缩的闸门立刻撑回去。
                            await gate.restore()
                        state = (
                            ItemState.CONVERGED
                            if outcome.converged
                            else ItemState.FAILED
                        )
                        return BatchResult(
                            loop_id=item.loop_id,
                            item_index=index,
                            state=state,
                            outcome=outcome,
                        )
                    except Exception as exc:
                        if _is_rate_limited(exc) and attempt < cfg.max_retries - 1:
                            # 429：降并发 + 指数退避 + 抖动重试
                            new_cap = await gate.downscale()
                            retry_after = getattr(exc, "retry_after", None)
                            ra = (
                                retry_after
                                if isinstance(retry_after, float) and retry_after > 0
                                else None
                            )
                            delay = _backoff_delay(attempt, ra)
                            self._rate_monitor.note_rate_limited(ra)
                            logger.warning(
                                "rate limited, downscaling and retrying",
                                extra={
                                    "loop_id": item.loop_id,
                                    "attempt": attempt + 1,
                                    "delay": round(delay, 3),
                                    "new_cap": new_cap,
                                },
                            )
                            await asyncio.sleep(delay)
                            attempt += 1
                            continue
                        # 非 429 或重试耗尽
                        return BatchResult(
                            loop_id=item.loop_id,
                            item_index=index,
                            state=ItemState.FAILED,
                            outcome=None,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                # 重试耗尽（循环正常退出）
                return BatchResult(
                    loop_id=item.loop_id,
                    item_index=index,
                    state=ItemState.FAILED,
                    outcome=None,
                    error="max retries exceeded",
                )

        tasks = [
            asyncio.create_task(_run_item(item, i)) for i, item in enumerate(items)
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, raw in enumerate(raw_results):
            if isinstance(raw, BatchResult):
                results[i] = raw
            else:
                # gather 的 return_exceptions 捕获的未预期异常
                results[i] = BatchResult(
                    loop_id=items[i].loop_id,
                    item_index=i,
                    state=ItemState.FAILED,
                    outcome=None,
                    error=f"unexpected: {type(raw).__name__}: {raw}",
                )

        final = [r for r in results if r is not None]
        converged = sum(1 for r in final if r.state is ItemState.CONVERGED)
        failed = sum(1 for r in final if r.state is ItemState.FAILED)
        skipped = sum(1 for r in final if r.state is ItemState.SKIPPED)

        return BatchReport(
            batch_id=batch_id,
            total=total,
            converged=converged,
            failed=failed,
            skipped=skipped,
            results=tuple(final),
            peak_concurrency=peak_concurrency,
        )


__all__ = [
    "BatchConfig",
    "BatchItem",
    "BatchReport",
    "BatchResult",
    "ConcurrencyGate",
    "DefaultRateLimitMonitor",
    "DefaultSandboxPoolMonitor",
    "ItemState",
    "LoopEngineFactory",
    "ParallelLoopPool",
    "RateLimitMonitor",
    "SandboxPoolMonitor",
]
