"""Loop 模式抽象。

模式是"断言类型 + 退出策略 + 重试节奏"的预设组合（docs/03 第 10 节）。
通过 registry 注册，与 telemetry.adapters / eval_module / verifier 同构。

为什么把模式差异抽成 BaseLoopMode 而非在 engine 里写 `if mode == ...`：
engine 的核心循环对所有模式相同（状态机驱动），差异只在少数决策点。
把差异点收拢到钩子，engine 才能保持单一职责 + 可穷尽测试；往 engine 里
塞分支会让状态机的正确性测试被模式逻辑污染。

四种模式（docs/03 第 10 节）：

| 模式 | 反馈信号 | 终止条件 | 特殊行为 |
|---|---|---|---|
| Retry | 错误码/异常 | 成功或达最大重试 | 指数退避 + 抖动；区分可重试/不可重试 |
| Quality | 质量评分 | 评分 ≥ 阈值 | 每轮带 critique；启用收益递减 |
| Verify-Execute | 测试/执行结果 | 全部验证通过 | 输出用 diff；强制沙箱 |
| HITL | 人工审批 | 批准或拒绝 | 转 HUMAN_PENDING；支持超时自动拒绝 |
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from ariadne.loop_module.context import OutputMode
from ariadne.loop_module.goal import Goal


@dataclass(frozen=True)
class RetryDecision:
    """执行失败后的处置决策。

    Retry 模式区分可重试（provider 抖动、限流）与不可重试（schema 不匹配），
    前者指数退避后重跑本轮，后者交回 JUDGING 走修正路径。
    """

    retry: bool
    delay_seconds: float = 0.0
    reason: str = ""


class BaseLoopMode(ABC):
    """模式钩子基类。

    engine 在少数决策点调用这些钩子。默认实现给出"Quality 模式行为"，
    子类按需覆盖。这样即便只注册了部分模式，engine 也有可用的默认值。
    """

    name: ClassVar[str]
    # 基线模型。engine 在预算降级时调 model_for(degraded=True) 取更便宜模型
    base_model: ClassVar[str] = "default"

    @abstractmethod
    def output_mode(self, goal: Goal) -> OutputMode:
        """上一轮输出的呈现方式。代码类场景用 diff，内容类用全文。"""

    def should_retry(self, error: BaseException, attempt: int) -> RetryDecision:
        """执行失败时是否原地重试。

        默认不重试：失败交回 JUDGING 走 critique 修正路径。
        Retry 模式覆盖此方法实现指数退避 + 可重试判定。
        """
        return RetryDecision(retry=False, reason="默认模式不原地重试")

    def model_for(self) -> str:
        """预算降级时取更便宜模型。M3 用静态映射，M5 接 provider 余量。"""
        return self.base_model

    def requires_pre_approval(self, goal: Goal) -> bool:
        """是否在执行前就需要人工审批。HITL 模式可能为 True。"""
        return False


__all__ = [
    "BaseLoopMode",
    "RetryDecision",
]
