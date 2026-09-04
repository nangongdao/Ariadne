"""正则类评估器：必含 / 必不含 / 引用计数 / Markdown 结构。

所有正则都必须避免嵌套量词（防 ReDoS）。这些评估器会在 M3 被 Loop
每轮调用，一个病态正则能让整个 Loop 卡死。
"""

from __future__ import annotations

import re
from typing import ClassVar, Final

from ariadne.eval_module import register_evaluator
from ariadne.eval_module.base import (
    BaseEvaluator,
    EvalContext,
    EvalResult,
    EvaluatorKind,
    truncate_evidence,
)

# 正则匹配的输入上限。超长文本上跑正则是 ReDoS 的主要风险面，
# 且评测超长输出本身就该在更上游被 Harness 拦掉。
MAX_INPUT_CHARS: Final = 200_000


@register_evaluator("regex")
class RegexEvaluator(BaseEvaluator):
    """通用正则匹配。

    must_match=True  → 必须匹配到（如"必须以一级标题开头"）
    must_match=False → 必须匹配不到（如"不得出现禁用词"）
    """

    kind: ClassVar[EvaluatorKind] = EvaluatorKind.DETERMINISTIC

    def __init__(
        self,
        name: str = "regex",
        *,
        pattern: str,
        must_match: bool = True,
        flags: int = 0,
        target: str = "output",
    ) -> None:
        super().__init__(name)
        self._pattern = re.compile(pattern, flags)
        self._must_match = must_match
        self._target = target

    def _pick(self, ctx: EvalContext) -> str:
        text = ctx.output if self._target == "output" else ctx.input
        return text[:MAX_INPUT_CHARS]

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        text = self._pick(ctx)
        found = self._pattern.search(text)
        passed = bool(found) if self._must_match else not found

        if passed:
            evidence = ""
        elif self._must_match:
            evidence = f"未匹配到 /{self._pattern.pattern}/"
        else:
            assert found is not None
            evidence = f"匹配到不应出现的内容: {found.group(0)[:200]!r}"

        return EvalResult(
            name=self.name,
            value=1.0 if passed else 0.0,
            passed=passed,
            evidence=evidence,
        )


# Markdown 链接、脚注、裸 URL、方括号编号四种引用形式。
# 分开写而非一个大正则：可分别统计，且避免嵌套量词。
_CITATION_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("markdown_link", re.compile(r"\[[^\]\n]{1,200}\]\(https?://[^\s)]{1,500}\)")),
    ("footnote", re.compile(r"\[\^[\w-]{1,40}\]")),
    ("bracket_number", re.compile(r"\[\d{1,3}\]")),
    ("bare_url", re.compile(r"(?<![(<\w])https?://[^\s<>）)]{4,500}")),
)


def count_citations(text: str) -> dict[str, int]:
    """按形式分别计数。

    去重按 URL/标记文本，避免同一来源引用多次被算成多个来源 ——
    "至少 3 个引用来源"的语义是 3 个不同来源。
    """
    counts: dict[str, int] = {}
    seen: set[str] = set()
    for label, pattern in _CITATION_PATTERNS:
        unique = set()
        for match in pattern.finditer(text[:MAX_INPUT_CHARS]):
            token = match.group(0)
            if token not in seen:
                seen.add(token)
                unique.add(token)
        counts[label] = len(unique)
    return counts


@register_evaluator("citation_count")
class CitationCountEvaluator(BaseEvaluator):
    """引用来源计数。

    "引用必带"是 docs/04 里的示例硬约束，也是内容生成场景最常用的断言。
    """

    kind: ClassVar[EvaluatorKind] = EvaluatorKind.DETERMINISTIC

    def __init__(self, name: str = "citation_count", *, min_count: int = 3) -> None:
        super().__init__(name)
        self._min_count = min_count

    @property
    def value_range(self) -> tuple[float, float]:
        return (0.0, float("inf"))

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        breakdown = count_citations(ctx.output)
        total = sum(breakdown.values())
        passed = total >= self._min_count

        return EvalResult(
            name=self.name,
            value=float(total),
            passed=passed,
            evidence=(
                ""
                if passed
                else f"引用数 {total} < 要求 {self._min_count}；"
                f"明细 {breakdown}"
            ),
        )


_MD_HEADING = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)
_MD_FENCE_OPEN = re.compile(r"^```(\w*)", re.MULTILINE)


@register_evaluator("markdown_structure")
class MarkdownStructureEvaluator(BaseEvaluator):
    """Markdown 结构校验：标题、代码块闭合、代码块语言标注。

    这些是 LLM 输出 Markdown 时最常犯的错，且都能确定性检测 ——
    典型的"不该用 Judge"的场景。
    """

    kind: ClassVar[EvaluatorKind] = EvaluatorKind.DETERMINISTIC

    def __init__(
        self,
        name: str = "markdown_structure",
        *,
        require_h1: bool = True,
        require_fence_language: bool = True,
        min_headings: int = 1,
    ) -> None:
        super().__init__(name)
        self._require_h1 = require_h1
        self._require_fence_language = require_fence_language
        self._min_headings = min_headings

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        text = ctx.output[:MAX_INPUT_CHARS]
        problems: list[str] = []

        headings = _MD_HEADING.findall(text)
        if len(headings) < self._min_headings:
            problems.append(f"标题数 {len(headings)} < 要求 {self._min_headings}")

        if self._require_h1 and not text.lstrip().startswith("# "):
            problems.append("未以一级标题开头")

        fences = _MD_FENCE_OPEN.findall(text)
        # 围栏总数为奇数说明有未闭合的代码块
        if len(fences) % 2 != 0:
            problems.append(f"代码块未闭合（发现 {len(fences)} 个围栏）")
        elif self._require_fence_language:
            # 偶数位是开围栏，奇数位是闭围栏；只检查开围栏的语言标注
            missing = sum(1 for lang in fences[::2] if not lang)
            if missing:
                problems.append(f"{missing} 个代码块缺少语言标注")

        return EvalResult(
            name=self.name,
            value=0.0 if problems else 1.0,
            passed=not problems,
            evidence=truncate_evidence("; ".join(problems)),
        )
