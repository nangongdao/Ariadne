"""Critique Synthesizer —— 结构化修正指令。

最常见的错误做法是把整个 eval 结果 JSON 原样塞回模型。三个后果：
上下文膨胀、注意力被无关信息稀释、模型可能"讨好分数"而非解决问题。

生成规则（见 docs/03）：
1. 证据必须截断 —— 一个 stack trace 能吃掉整个上下文预算
2. directives 优先来自 Assertion.hint —— 人工预设的提示效果通常远好于
   模型自己总结
3. forbidden 是防振荡的关键 —— 把"这些路走过了"显式列出
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ariadne.eval_module.base import truncate_evidence
from ariadne.loop_module.fingerprint import OscillationReport, OscillationVerdict
from ariadne.loop_module.goal import Assertion, AssertionKind, Goal
from ariadne.loop_module.verifier.base import AssertionOutcome, Verdict

MAX_FAILURES_LISTED = 8
MAX_EVIDENCE_LINES_HEAD = 12
MAX_EVIDENCE_LINES_TAIL = 4
MAX_FORBIDDEN_ITEMS = 5

# 各断言类型的兜底修正指令。仅在 Assertion.hint 缺失时使用 ——
# 人工提示更具体，模板只是保证"总有话可说"。
_FALLBACK_DIRECTIVES: dict[AssertionKind, str] = {
    AssertionKind.COMMAND: "修正代码使该命令成功执行，先看报错定位根因再改",
    AssertionKind.SCHEMA: "严格按 Schema 输出合法 JSON，不要加围栏外的说明文字",
    AssertionKind.REGEX: "调整输出格式以满足该模式要求",
    AssertionKind.METRIC: "针对该指标的薄弱处改进，而非整体重写",
    AssertionKind.HUMAN: "等待人工审批，无需修改",
}


@dataclass(frozen=True)
class Critique:
    """结构化修正指令。刻意不是自由文本。"""

    # 具体失败项（人话描述，不是 assertion id）
    failures: tuple[str, ...] = ()
    # 截断后的证据片段
    evidence: tuple[str, ...] = ()
    # 明确的修正动作
    directives: tuple[str, ...] = ()
    # 上几轮试过且失败的方向，避免重复
    forbidden: tuple[str, ...] = ()
    # 策略升级提示（振荡时非空）
    escalation: str = ""

    @property
    def is_empty(self) -> bool:
        return not (self.failures or self.directives)

    def render(self) -> str:
        """渲染为喂给模型的文本。

        分段而非一整块：模型对结构化分段的遵循度明显更好。
        """
        if self.is_empty:
            return ""

        blocks: list[str] = []

        if self.failures:
            items = "\n".join(f"  {i}. {f}" for i, f in enumerate(self.failures, 1))
            blocks.append(f"## 未通过的检查\n{items}")

        if self.evidence:
            joined = "\n\n".join(self.evidence)
            blocks.append(f"## 具体证据\n{joined}")

        if self.directives:
            items = "\n".join(f"  - {d}" for d in self.directives)
            blocks.append(f"## 本轮需要做的修正\n{items}")

        if self.forbidden:
            items = "\n".join(f"  - {f}" for f in self.forbidden)
            blocks.append(
                f"## 已尝试且失败的方向（不要重复）\n{items}"
            )

        if self.escalation:
            blocks.append(f"## 注意\n{self.escalation}")

        return "\n\n".join(blocks)


def _describe_failure(outcome: AssertionOutcome, assertion: Assertion | None) -> str:
    """人话描述失败项。不暴露 assertion id 与内部 spec 结构。"""
    if assertion is None:
        return f"检查 {outcome.assertion_id} 未通过"
    return assertion.describe() + " —— 未满足"


def _directive_for(assertion: Assertion | None, outcome: AssertionOutcome) -> str:
    """修正指令。优先用人工 hint。"""
    if assertion is not None and assertion.hint.strip():
        return assertion.hint.strip()
    if assertion is not None:
        return _FALLBACK_DIRECTIVES[assertion.kind]
    return f"修正 {outcome.assertion_id} 相关问题"


_ESCALATION_MESSAGES: dict[OscillationVerdict, str] = {
    OscillationVerdict.ESCALATE: (
        "前几轮的修改方向没有奏效。**换一种解法**，不要在同一处反复微调。"
    ),
    OscillationVerdict.OSCILLATING: (
        "本轮输出与之前某轮完全相同，说明陷入了循环。"
        "**必须采用不同的实现思路**，而非重新表述同样的内容。"
    ),
}


class CritiqueSynthesizer:
    """从裁决与振荡报告生成修正指令。"""

    def __init__(
        self,
        *,
        max_failures: int = MAX_FAILURES_LISTED,
        max_forbidden: int = MAX_FORBIDDEN_ITEMS,
    ) -> None:
        self._max_failures = max_failures
        self._max_forbidden = max_forbidden

    def synthesize(
        self,
        verdict: Verdict,
        goal: Goal,
        *,
        oscillation: OscillationReport | None = None,
    ) -> Critique:
        # 只对真正失败的断言生成指令。
        # errored 的不生成 —— 那是配置/环境问题，让模型去"修"是无效指令。
        actionable = [
            o for o in verdict.failed if o.hint_worthy
        ][: self._max_failures]

        failures: list[str] = []
        evidence: list[str] = []
        directives: list[str] = []
        seen_directives: set[str] = set()

        for outcome in actionable:
            assertion = goal.assertion_by_id(outcome.assertion_id)
            failures.append(_describe_failure(outcome, assertion))

            if outcome.evidence:
                evidence.append(
                    truncate_evidence(
                        outcome.evidence,
                        head=MAX_EVIDENCE_LINES_HEAD,
                        tail=MAX_EVIDENCE_LINES_TAIL,
                    )
                )

            directive = _directive_for(assertion, outcome)
            if directive not in seen_directives:
                seen_directives.add(directive)
                directives.append(directive)

        # errored 单独说明：让用户知道有检查没跑成，而非静默忽略
        if verdict.errored:
            failures.append(
                f"以下检查未能执行（配置或环境问题，非输出质量问题）："
                f"{', '.join(verdict.errored)}"
            )

        forbidden = (
            oscillation.forbid_hints[: self._max_forbidden]
            if oscillation
            else ()
        )
        escalation = (
            _ESCALATION_MESSAGES.get(oscillation.verdict, "")
            if oscillation
            else ""
        )

        return Critique(
            failures=tuple(failures),
            evidence=tuple(evidence),
            directives=tuple(directives),
            forbidden=tuple(forbidden),
            escalation=escalation,
        )


def summarize_history(
    critiques: Sequence[Critique], *, max_lines: int = 5
) -> str:
    """历史压缩。

    只保留失败摘要，不保留完整输出 —— 上下文收敛的核心手段
    （见 context.py 的四段结构）。
    """
    lines: list[str] = []
    for index, critique in enumerate(critiques, start=1):
        if not critique.failures:
            continue
        first = critique.failures[0]
        lines.append(f"轮次 {index}：{first[:120]}")

    if len(lines) <= max_lines:
        return "\n".join(lines)

    # 超出上限时把早期若干轮合并为一行趋势描述
    kept = lines[-max_lines:]
    omitted = len(lines) - max_lines
    return "\n".join([f"（前 {omitted} 轮的失败已省略）", *kept])
