"""上下文收敛策略。

Loop 的 Token 消耗随轮次线性甚至超线性增长，根因是"把全量历史都塞进去"。
收敛策略把每轮上下文固定为四段：

  [固定] 任务规格          ← 每轮不变，放最前面以命中 provider prompt 缓存
  [最新] 上一轮输出/diff    ← 代码类用 diff，内容类用全文
  [聚焦] 本轮 Critique      ← 结构化，长度可控
  [压缩] 历史失败签名摘要    ← 只保留"试过什么、为什么不行"

三条量化约束：
  - 总量不超过 max_tokens_per_iteration 的 60%，剩余留给输出
  - 历史摘要只保留失败签名，不保留完整输出
  - 超过 5 轮启用二次压缩
"""

from __future__ import annotations

import difflib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from ariadne.loop_module.critique import Critique, summarize_history
from ariadne.loop_module.goal import Goal

# 实体在 utils.tokens：Harness 规则侧（token_count）也要用，留在本模块会让
# 规则求值把整个 loop_module 包拖进来。此处只是本模块自用，不作转出 ——
# 外部一律直接从 ariadne.utils.tokens 取。
from ariadne.utils.tokens import CHARS_PER_TOKEN, estimate_tokens

# 上下文占单轮预算的比例上限，剩余留给输出
CONTEXT_BUDGET_RATIO = 0.6
# 超过此轮次启用二次压缩
SECONDARY_COMPRESSION_AFTER = 5


class OutputMode(StrEnum):
    """上一轮输出的呈现方式。"""

    FULL = "full"
    DIFF = "diff"


@dataclass(frozen=True)
class ContextSegments:
    """四段结构。分开保存以便按段裁剪与统计。"""

    spec: str
    last_output: str
    critique: str
    history: str

    def render(self) -> str:
        blocks = [b for b in (self.spec, self.last_output, self.critique, self.history) if b]
        return "\n\n---\n\n".join(blocks)

    def token_breakdown(self) -> dict[str, int]:
        return {
            "spec": estimate_tokens(self.spec),
            "last_output": estimate_tokens(self.last_output),
            "critique": estimate_tokens(self.critique),
            "history": estimate_tokens(self.history),
        }

    @property
    def total_tokens(self) -> int:
        return sum(self.token_breakdown().values())


def make_diff(previous: str, current: str, *, context_lines: int = 3) -> str:
    """生成统一 diff。

    代码类场景用 diff 而非全文：改一行的 3000 行文件，全文会占满上下文，
    而 diff 只有几行。
    """
    diff = difflib.unified_diff(
        previous.splitlines(keepends=False),
        current.splitlines(keepends=False),
        fromfile="上一轮",
        tofile="本轮",
        n=context_lines,
        lineterm="",
    )
    return "\n".join(diff)


@dataclass
class ContextBuilder:
    """构造每轮上下文。

    goal 与 output_mode 在 Loop 生命周期内固定，历史随轮次累积。
    """

    goal: Goal
    output_mode: OutputMode = OutputMode.FULL

    def build(
        self,
        *,
        iteration: int,
        last_output: str = "",
        previous_output: str = "",
        critique: Critique | None = None,
        history: Sequence[Critique] = (),
    ) -> ContextSegments:
        budget = self._context_budget()

        spec = self.goal.spec_summary()
        output_block = self._format_output(last_output, previous_output)
        critique_block = critique.render() if critique else ""
        history_block = self._format_history(history, iteration)

        segments = ContextSegments(
            spec=spec,
            last_output=output_block,
            critique=critique_block,
            history=history_block,
        )

        if segments.total_tokens <= budget:
            return segments
        return self._shrink(segments, budget)

    def _context_budget(self) -> int:
        return int(
            self.goal.budget.max_tokens_per_iteration * CONTEXT_BUDGET_RATIO
        )

    def _format_output(self, last_output: str, previous_output: str) -> str:
        if not last_output:
            return ""

        if self.output_mode is OutputMode.DIFF and previous_output:
            diff = make_diff(previous_output, last_output)
            # diff 为空说明输出没变（振荡检测会另行报告），
            # 此时给全文更有用 —— 让模型看到当前状态
            if diff.strip():
                return f"## 相对上一轮的改动\n```diff\n{diff}\n```"

        return f"## 上一轮输出\n{last_output}"

    def _format_history(self, history: Sequence[Critique], iteration: int) -> str:
        if not history:
            return ""

        # 超过阈值时收紧摘要行数（二次压缩）
        max_lines = 3 if iteration > SECONDARY_COMPRESSION_AFTER else 5
        summary = summarize_history(history, max_lines=max_lines)
        if not summary:
            return ""
        return f"## 历史失败摘要\n{summary}"

    def _shrink(self, segments: ContextSegments, budget: int) -> ContextSegments:
        """超预算时按优先级裁剪。

        裁剪顺序：历史 → 上一轮输出 → 证据。
        **spec 与 critique 绝不裁剪** —— 前者是任务定义，后者是本轮要改什么，
        砍掉任何一个都会让这一轮变成瞎猜。
        """
        output = segments.last_output

        # 1. 先砍历史。本方法只在超预算时被调用，因此无条件丢弃 ——
        # 历史摘要是四段里信息密度最低的（失败签名已在 critique 的
        # forbidden 段体现），优先牺牲它。
        candidate = ContextSegments(
            spec=segments.spec,
            last_output=output,
            critique=segments.critique,
            history="",
        )
        if candidate.total_tokens <= budget:
            return candidate

        # 2. 再截断上一轮输出。保留头尾 —— 中间部分通常是重复内容
        fixed = estimate_tokens(segments.spec) + estimate_tokens(segments.critique)
        remaining = max(budget - fixed, 0)
        if remaining <= 0:
            # 极端情况：spec + critique 就超了。保留它们，输出全砍。
            return ContextSegments(
                spec=segments.spec,
                last_output="（上一轮输出因上下文预算不足已省略）",
                critique=segments.critique,
                history="",
            )

        return ContextSegments(
            spec=segments.spec,
            last_output=_truncate_middle(output, remaining),
            critique=segments.critique,
            history="",
        )


def _truncate_middle(text: str, token_budget: int) -> str:
    """保留头尾、省略中间。

    中间部分通常是重复的样板内容，而头部有结构信息、尾部有结论。
    """
    char_budget = int(token_budget * CHARS_PER_TOKEN)
    if len(text) <= char_budget:
        return text

    half = max(char_budget // 2 - 40, 100)
    omitted = len(text) - half * 2
    return f"{text[:half]}\n\n…（省略 {omitted} 字符）…\n\n{text[-half:]}"


def cache_prefix(goal: Goal) -> str:
    """可被 provider 缓存的固定前缀。

    把它放在每轮 prompt 最前面：多轮场景下缓存命中能显著压低实际计费。
    缓存命中的 Token 按折扣价计量（M1 的 pricing 已支持），
    否则预算熔断会误触发。
    """
    return goal.spec_summary()
