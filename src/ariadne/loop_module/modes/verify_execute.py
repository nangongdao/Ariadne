"""Verify-Execute 模式 —— 代码生成、数据分析场景。

反馈信号：测试/执行结果（COMMAND 类断言，退出码二值无歧义）。
退出条件：全部验证通过。输出用 diff 而非全文——改一行的 3000 行文件，
全文会占满上下文，diff 只有几行（docs/03 第 6 节）。

M3 阶段用受限子进程（restricted_exec），M4 换 gVisor 沙箱。
"""

from __future__ import annotations

from ariadne.loop_module.context import OutputMode
from ariadne.loop_module.goal import Goal
from ariadne.loop_module.modes.base import BaseLoopMode
from ariadne.loop_module.modes.registry import register_loop_mode


@register_loop_mode("verify_execute")
class VerifyExecuteMode(BaseLoopMode):
    name = "verify_execute"
    base_model = "code-default"

    def output_mode(self, goal: Goal) -> OutputMode:
        # 代码类用 diff：让模型聚焦"这轮改了什么"，而非重读全文
        return OutputMode.DIFF


__all__ = ["VerifyExecuteMode"]
