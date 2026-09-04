"""Quality 模式 —— 内容生成质量不达标时迭代。

反馈信号：质量评分（METRIC 类断言）。退出条件：评分 ≥ 阈值。
每轮必带 critique；启用收益递减检测（已在 OscillationDetector 实现）。
"""

from __future__ import annotations

from ariadne.loop_module.context import OutputMode
from ariadne.loop_module.goal import Goal
from ariadne.loop_module.modes.base import BaseLoopMode
from ariadne.loop_module.modes.registry import register_loop_mode


@register_loop_mode("quality")
class QualityMode(BaseLoopMode):
    name = "quality"
    base_model = "quality-default"

    def output_mode(self, goal: Goal) -> OutputMode:
        # 内容类用全文：评分要看整体表达，diff 会丢失上下文。
        return OutputMode.FULL


__all__ = ["QualityMode"]
