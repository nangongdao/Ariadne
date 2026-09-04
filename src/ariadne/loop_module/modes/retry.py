"""Retry 模式 —— API 失败、格式错误时重试。

反馈信号：错误码/异常。退出条件：成功或达最大重试次数。
特殊行为：指数退避 + 抖动；区分可重试/不可重试错误。

可重试：provider 速率限制（429）、临时网络抖动、超时。
不可重试：schema 不匹配、认证失败、目标本身不可达 —— 重试只是重复同样的错，
应交回 JUDGING 走 critique 修正路径，而非浪费预算。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import httpx

from ariadne.loop_module.context import OutputMode
from ariadne.loop_module.goal import Goal
from ariadne.loop_module.modes.base import BaseLoopMode, RetryDecision
from ariadne.loop_module.modes.registry import register_loop_mode
from ariadne.runtime_module.llm.errors import LLMRateLimitError

# 退避参数。刻意不含在 Goal.budget 里：这是重试节奏，不是预算维度。
BASE_DELAY = 1.0
MAX_DELAY = 30.0
JITTER = 0.25

# 可重试的异常类型。其余视为不可重试，避免对永久性错误反复烧预算。
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    LLMRateLimitError,
)

# HTTP 状态码层面可重试的状态：429 速率限制 + 5xx 临时性服务端故障。
# 其余（400/401/403/404 等）是确定性错误，重试只会重复同样的错。
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryConfig:
    base_delay: float = BASE_DELAY
    max_delay: float = MAX_DELAY
    jitter: float = JITTER
    max_retries: int = 5


@register_loop_mode("retry")
class RetryMode(BaseLoopMode):
    name = "retry"
    base_model = "retry-default"

    def __init__(self, config: RetryConfig | None = None) -> None:
        self._config = config or RetryConfig()

    def output_mode(self, goal: Goal) -> OutputMode:
        return OutputMode.FULL

    def should_retry(self, error: BaseException, attempt: int) -> RetryDecision:
        # 达到上限：再多试也是重复同样的错，让 JUDGING 接手
        if attempt >= self._config.max_retries:
            return RetryDecision(
                retry=False, reason=f"已达重试上限 {self._config.max_retries}"
            )
        if isinstance(error, RETRYABLE_EXCEPTIONS):
            return RetryDecision(
                retry=True,
                delay_seconds=self._retry_delay(error, attempt),
                reason=f"可重试错误: {type(error).__name__}",
            )
        if isinstance(error, httpx.HTTPStatusError) and (
            error.response.status_code in _RETRYABLE_STATUS
        ):
            return RetryDecision(
                retry=True,
                delay_seconds=self._retry_delay(error, attempt),
                reason=f"可重试状态码: HTTP {error.response.status_code}",
            )
        return RetryDecision(
            retry=False, reason=f"不可重试错误: {type(error).__name__}"
        )

    def _retry_delay(self, error: BaseException, attempt: int) -> float:
        """退避节奏。provider 明示 Retry-After 时取两者较大值——
        既尊重 provider 指示的下限，也不低于自身退避节奏。"""
        backoff = self._backoff(attempt)
        retry_after = getattr(error, "retry_after", None)
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            return max(float(retry_after), backoff)
        return backoff

    def _backoff(self, attempt: int) -> float:
        """指数退避 + 抖动。加抖动避免多 Worker 同步重试打爆 provider。"""
        delay = min(self._config.base_delay * (2**attempt), self._config.max_delay)
        jitter = delay * self._config.jitter
        # random.uniform 在 [delay - jitter, delay + jitter]，不引入配置
        return max(0.0, random.uniform(delay - jitter, delay + jitter))


__all__ = ["RetryConfig", "RetryMode"]
