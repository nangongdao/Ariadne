"""数值类评估器：字数、长度、数值范围、禁用词。"""

from __future__ import annotations

import re
from typing import ClassVar, Final

from ariadne.eval_module import register_evaluator
from ariadne.eval_module.base import (
    BaseEvaluator,
    EvalContext,
    EvalResult,
    EvaluatorKind,
    ThresholdOp,
    compare,
)

# 中文按字符计、西文按词计。混排文本两者相加 ——
# 对"不超过 800 字"这类约束，把中文按词算会严重低估。
_CJK = re.compile(r"[一-鿿㐀-䶿]")
_WORD = re.compile(r"[A-Za-z0-9]+(?:['’-][A-Za-z0-9]+)*")

MAX_TEXT_CHARS: Final = 500_000


def count_words(text: str) -> int:
    """混排文本的字数。中文字符数 + 西文词数。"""
    sample = text[:MAX_TEXT_CHARS]
    return len(_CJK.findall(sample)) + len(_WORD.findall(sample))


@register_evaluator("word_count")
class WordCountEvaluator(BaseEvaluator):
    """字数区间校验。

    区间而非单侧阈值：内容生成场景通常两头都有要求
    （太短没信息量，太长超出载体限制）。
    """

    kind: ClassVar[EvaluatorKind] = EvaluatorKind.DETERMINISTIC

    def __init__(
        self,
        name: str = "word_count",
        *,
        min_words: int = 0,
        max_words: int | None = None,
    ) -> None:
        super().__init__(name)
        self._min = min_words
        self._max = max_words

    @property
    def value_range(self) -> tuple[float, float]:
        return (0.0, float(self._max) if self._max else float("inf"))

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        count = count_words(ctx.output)
        too_short = count < self._min
        too_long = self._max is not None and count > self._max
        passed = not (too_short or too_long)

        if too_short:
            evidence = f"字数 {count} < 下限 {self._min}"
        elif too_long:
            evidence = f"字数 {count} > 上限 {self._max}"
        else:
            evidence = ""

        return EvalResult(
            name=self.name, value=float(count), passed=passed, evidence=evidence
        )


@register_evaluator("numeric_range")
class NumericRangeEvaluator(BaseEvaluator):
    """从输出中提取数值并比较。

    用途：要求模型输出的某个数值指标落在合理区间
    （如"预估耗时"不能是负数）。
    """

    kind: ClassVar[EvaluatorKind] = EvaluatorKind.DETERMINISTIC

    def __init__(
        self,
        name: str = "numeric_range",
        *,
        pattern: str = r"-?\d+(?:\.\d+)?",
        op: ThresholdOp = ThresholdOp.GTE,
        threshold: float = 0.0,
        occurrence: int = 0,
    ) -> None:
        super().__init__(name)
        self._pattern = re.compile(pattern)
        self._op = op
        self._threshold = threshold
        self._occurrence = occurrence

    @property
    def value_range(self) -> tuple[float, float]:
        return (float("-inf"), float("inf"))

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        matches = self._pattern.findall(ctx.output[:MAX_TEXT_CHARS])
        if len(matches) <= self._occurrence:
            return EvalResult(
                name=self.name,
                value=0.0,
                passed=False,
                evidence=f"未找到第 {self._occurrence + 1} 个匹配的数值",
            )

        raw = matches[self._occurrence]
        # findall 在有分组时返回元组，取第一个非空分组
        if isinstance(raw, tuple):
            raw = next((g for g in raw if g), "")

        try:
            value = float(raw)
        except ValueError:
            return EvalResult(
                name=self.name,
                value=0.0,
                passed=False,
                evidence=f"提取到的 {raw!r} 无法转为数值",
            )

        passed = compare(value, self._op, self._threshold)
        return EvalResult(
            name=self.name,
            value=value,
            passed=passed,
            evidence="" if passed else f"{value} 不满足 {self._op.value} {self._threshold}",
        )


@register_evaluator("forbidden_terms")
class ForbiddenTermsEvaluator(BaseEvaluator):
    """禁用词检测。

    大小写不敏感 + 全词匹配（避免 "AI" 命中 "SAID"）。
    中文无词边界概念，故对含 CJK 的词退化为子串匹配。
    """

    kind: ClassVar[EvaluatorKind] = EvaluatorKind.DETERMINISTIC

    def __init__(
        self,
        name: str = "forbidden_terms",
        *,
        terms: tuple[str, ...],
        case_sensitive: bool = False,
    ) -> None:
        super().__init__(name)
        self._terms = terms
        self._case_sensitive = case_sensitive
        flags = 0 if case_sensitive else re.IGNORECASE
        self._patterns = tuple(
            (term, re.compile(self._build(term), flags)) for term in terms
        )

    @staticmethod
    def _build(term: str) -> str:
        escaped = re.escape(term)
        # 中文无词边界，\b 在 CJK 边界上行为不符预期
        if _CJK.search(term):
            return escaped
        return rf"\b{escaped}\b"

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        text = ctx.output[:MAX_TEXT_CHARS]
        hits = [term for term, pattern in self._patterns if pattern.search(text)]

        return EvalResult(
            name=self.name,
            value=0.0 if hits else 1.0,
            passed=not hits,
            evidence="" if not hits else f"出现禁用词: {', '.join(hits)}",
        )


@register_evaluator("exact_match")
class ExactMatchEvaluator(BaseEvaluator):
    """与参考答案精确匹配。

    需要 expected。用于有唯一正确答案的场景（分类、抽取）。
    """

    kind: ClassVar[EvaluatorKind] = EvaluatorKind.DETERMINISTIC

    def __init__(
        self,
        name: str = "exact_match",
        *,
        normalize_whitespace: bool = True,
        case_sensitive: bool = False,
    ) -> None:
        super().__init__(name)
        self._normalize = normalize_whitespace
        self._case_sensitive = case_sensitive

    def _norm(self, text: str) -> str:
        result = text.strip()
        if self._normalize:
            result = " ".join(result.split())
        return result if self._case_sensitive else result.lower()

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        if ctx.expected is None:
            return EvalResult(
                name=self.name,
                value=0.0,
                passed=False,
                evidence="exact_match 需要 expected，但样本未提供",
                errored=True,
            )

        matched = self._norm(ctx.output) == self._norm(ctx.expected)
        return EvalResult(
            name=self.name,
            value=1.0 if matched else 0.0,
            passed=matched,
            evidence=""
            if matched
            else f"期望 {self._norm(ctx.expected)[:200]!r}，"
            f"实际 {self._norm(ctx.output)[:200]!r}",
        )
