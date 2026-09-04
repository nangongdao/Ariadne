"""工具执行的 Harness 包装层。

GuardedCommandRunner 在 pre_tool 卡点求值后再委托真实执行器，
与 runtime_module.llm.guarded 的 GuardedLLMAdapter 是同一形状。
"""

from __future__ import annotations

from ariadne.runtime_module.tool.guarded import GuardedCommandRunner

__all__ = [
    "GuardedCommandRunner",
]
