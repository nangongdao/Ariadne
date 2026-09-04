"""HITL 模式 —— 高敏感决策需人工审批。

反馈信号：人工审批（HUMAN 类断言）。退出条件：批准或拒绝。
特殊行为：转 HUMAN_PENDING 并持久化；支持超时自动拒绝（fail-closed）。

审批超时视为拒绝而非放行——这是 docs/03 与状态机共同强制的安全设计。
"""

from __future__ import annotations

from ariadne.loop_module.context import OutputMode
from ariadne.loop_module.goal import AssertionKind, Goal
from ariadne.loop_module.modes.base import BaseLoopMode
from ariadne.loop_module.modes.registry import register_loop_mode


@register_loop_mode("hitl")
class HITLMode(BaseLoopMode):
    name = "hitl"
    base_model = "hitl-default"

    def output_mode(self, goal: Goal) -> OutputMode:
        return OutputMode.FULL

    def requires_pre_approval(self, goal: Goal) -> bool:
        # 含 human 类断言时，执行前即转 HUMAN_PENDING 等审批
        return any(a.kind is AssertionKind.HUMAN for a in goal.assertions)


__all__ = ["HITLMode"]
