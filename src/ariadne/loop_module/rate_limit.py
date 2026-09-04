"""自适应速率余量监控 —— 并行 Loop 池的 provider 并发决策依据。

R9 缓解策略的"并发度由 provider 余量动态决定"部分：进程内共享的
余量估计器，由并行池喂入 429/成功信号。429 进入冷却期并折减余量
系数；成功逐步恢复。冷却期内 available_concurrency 返回 0——
暂停派发比排队等（退化成 barrier）或硬打（升级成封禁）都好。

2026-09-03 扩展：增加主动配额监控（quota_based_concurrency）。
Anthropic 等 provider 在成功响应头返回剩余配额，预测配额耗尽前
主动降并发，形成双重防线：配额预警（主动）+ 429 退避（被动）。

后续可换 Redis 共享实现做多 Worker 协调（协议缝已留好）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

# 余量系数下限：连续 429 三次后到底，保留探测流量
MIN_FACTOR = 0.125
# 每次成功恢复的系数步长：8 次连续成功从下限爬回满额
RECOVERY_STEP = 0.125
# 无 Retry-After 时的默认冷却秒数
DEFAULT_COOLDOWN = 30.0
# Retry-After 异常大时的上限，防单次 429 长时间停摆整个批次
_MAX_COOLDOWN = 120.0

# 主动配额监控阈值：剩余请求数低于此值时触发降并发
QUOTA_LOW_REQUESTS_THRESHOLD = 5
# 主动配额监控阈值：剩余 token 数低于此值时触发降并发（约 2-3 个请求的平均用量）
QUOTA_LOW_TOKENS_THRESHOLD = 10_000


class AdaptiveRateLimitMonitor:
    """基于 429/成功信号的自适应余量估计器。

    单事件循环内使用（并行池的场景）：方法体无 await，普通属性即可，
    无需加锁。跨线程/跨进程共享交给后续的 Redis 实现。

    用法：``monitor.note_rate_limited(error.retry_after)`` 喂入 429，
    ``monitor.note_success()`` 喂入成功，批次启动时
    ``available_concurrency(cfg.max_concurrency)`` 取动态并发上限。
    """

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._factor = 1.0
        self._cooldown_until = 0.0
        self._clock = clock or time.monotonic

    def note_rate_limited(self, retry_after: float | None = None) -> None:
        """429 信号：余量系数折半，进入冷却期。

        retry_after 来自 LLMRateLimitError（provider 的 Retry-After 头），
        无则用默认冷却。上限 _MAX_COOLDOWN 防止异常大的值长时间停摆。
        """
        self._factor = max(MIN_FACTOR, self._factor / 2)
        cooldown = DEFAULT_COOLDOWN if retry_after is None else min(
            retry_after, _MAX_COOLDOWN
        )
        self._cooldown_until = max(self._cooldown_until, self._clock() + cooldown)

    def note_success(self) -> None:
        """成功信号：余量逐步恢复（探针式回升，避免瞬间满额再撞墙）。"""
        self._factor = min(1.0, self._factor + RECOVERY_STEP)

    def available_concurrency(self, cap: int) -> int:
        """当前允许的并发数。冷却期内 0（暂停派发）；否则按系数折减取整。"""
        if self._clock() < self._cooldown_until:
            return 0
        return int(cap * self._factor)

    @property
    def factor(self) -> float:
        """当前余量系数（观测用）。"""
        return self._factor

    @property
    def cooling_down(self) -> bool:
        """是否处于冷却期（观测用）。"""
        return self._clock() < self._cooldown_until


def quota_based_concurrency(quota: Any, max_concurrency: int) -> int:
    """基于配额响应头计算建议并发度（主动余量调度）。

    Anthropic 等 provider 在成功响应头返回剩余配额（requests_remaining /
    tokens_remaining）。配额接近耗尽时主动降低并发，避免触发 429。

    决策逻辑：
    - requests_remaining < QUOTA_LOW_REQUESTS_THRESHOLD → 降并发到 1（保守派发）
    - tokens_remaining < QUOTA_LOW_TOKENS_THRESHOLD → 降并发到 1
    - 否则返回 max_concurrency（无需限制）

    quota 为 None 或不含配额字段时返回 max_concurrency（provider 不支持配额头，
    无法做主动调度，降级到被动 429 防线）。

    Args:
        quota: RateLimitQuota 实例或 None（LLMResponse.rate_limit_quota）
        max_concurrency: 配置的最大并发数

    Returns:
        建议的并发度（1 到 max_concurrency）
    """
    if quota is None:
        return max_concurrency

    # 动态读取字段（避免循环导入 RateLimitQuota）
    requests_remaining = getattr(quota, "requests_remaining", None)
    tokens_remaining = getattr(quota, "tokens_remaining", None)

    # 剩余请求数低：配额即将耗尽，降到保守派发
    if (
        requests_remaining is not None
        and requests_remaining < QUOTA_LOW_REQUESTS_THRESHOLD
    ):
        return 1

    # 剩余 token 数低：即使请求数够，token 配额也接近耗尽
    if tokens_remaining is not None and tokens_remaining < QUOTA_LOW_TOKENS_THRESHOLD:
        return 1

    return max_concurrency


__all__ = [
    "DEFAULT_COOLDOWN",
    "MIN_FACTOR",
    "QUOTA_LOW_REQUESTS_THRESHOLD",
    "QUOTA_LOW_TOKENS_THRESHOLD",
    "RECOVERY_STEP",
    "AdaptiveRateLimitMonitor",
    "quota_based_concurrency",
]
