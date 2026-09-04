"""统计类评估器：重合度与编辑距离。

用于有参考答案的回归测试。可信度 ★★★，比 Judge 便宜且确定，
但对"语义对但表述不同"的输出会误判低分 —— 因此适合回归而非质量评判。
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
    ThresholdOp,
    compare,
)

MAX_TEXT_CHARS: Final = 100_000

_CJK = re.compile(r"[一-鿿㐀-䶿]")
_TOKEN = re.compile(r"[A-Za-z0-9]+|[一-鿿㐀-䶿]")


def tokenize(text: str) -> list[str]:
    """混排分词。

    中文按字切分而非引入分词库：ROUGE 在中文上按字计算是常见做法，
    且避免为一个评估器引入 jieba 这类重依赖（还要管词典版本）。
    """
    return _TOKEN.findall(text[:MAX_TEXT_CHARS].lower())


def _lcs_length(a: list[str], b: list[str]) -> int:
    """最长公共子序列长度。

    滚动数组实现：完整 DP 表在长文本上是 O(n·m) 内存，
    两行滚动降到 O(min(n,m))。
    """
    if not a or not b:
        return 0
    # 让 b 是较短的那个，减少每行长度
    if len(b) > len(a):
        a, b = b, a

    previous = [0] * (len(b) + 1)
    for token_a in a:
        current = [0] * (len(b) + 1)
        for j, token_b in enumerate(b, start=1):
            if token_a == token_b:
                current[j] = previous[j - 1] + 1
            else:
                current[j] = max(previous[j], current[j - 1])
        previous = current
    return previous[len(b)]


@register_evaluator("rouge_l")
class RougeLEvaluator(BaseEvaluator):
    """ROUGE-L F1。

    基于 LCS，对语序敏感。适合摘要类任务的回归检测。
    """

    kind: ClassVar[EvaluatorKind] = EvaluatorKind.STATISTICAL

    def __init__(
        self,
        name: str = "rouge_l",
        *,
        threshold: float = 0.5,
        beta: float = 1.0,
    ) -> None:
        super().__init__(name)
        self._threshold = threshold
        self._beta = beta

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        if ctx.expected is None:
            return EvalResult(
                name=self.name,
                value=0.0,
                passed=False,
                evidence="rouge_l 需要 expected",
                errored=True,
            )

        hypothesis = tokenize(ctx.output)
        reference = tokenize(ctx.expected)
        if not hypothesis or not reference:
            return EvalResult(
                name=self.name,
                value=0.0,
                passed=False,
                evidence="输出或参考答案为空",
            )

        lcs = _lcs_length(hypothesis, reference)
        precision = lcs / len(hypothesis)
        recall = lcs / len(reference)

        if precision + recall == 0:
            score = 0.0
        else:
            beta_sq = self._beta**2
            score = ((1 + beta_sq) * precision * recall) / (
                recall + beta_sq * precision
            )

        passed = compare(score, ThresholdOp.GTE, self._threshold)
        return EvalResult(
            name=self.name,
            value=round(score, 4),
            passed=passed,
            evidence=""
            if passed
            else f"ROUGE-L {score:.3f} < 阈值 {self._threshold}"
            f"（P={precision:.3f} R={recall:.3f} LCS={lcs}）",
        )


@register_evaluator("token_f1")
class TokenF1Evaluator(BaseEvaluator):
    """词袋 F1（不考虑语序）。

    与 ROUGE-L 互补：内容对但顺序不同时 ROUGE-L 会偏低，
    此指标不受影响。两者一起看能区分"内容缺失"与"结构混乱"。
    """

    kind: ClassVar[EvaluatorKind] = EvaluatorKind.STATISTICAL

    def __init__(self, name: str = "token_f1", *, threshold: float = 0.5) -> None:
        super().__init__(name)
        self._threshold = threshold

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        if ctx.expected is None:
            return EvalResult(
                name=self.name,
                value=0.0,
                passed=False,
                evidence="token_f1 需要 expected",
                errored=True,
            )

        from collections import Counter

        hypothesis = Counter(tokenize(ctx.output))
        reference = Counter(tokenize(ctx.expected))
        if not hypothesis or not reference:
            return EvalResult(
                name=self.name, value=0.0, passed=False, evidence="输出或参考答案为空"
            )

        # 多重集交集：重复词按较小次数计，避免刷词提分
        overlap = sum((hypothesis & reference).values())
        precision = overlap / sum(hypothesis.values())
        recall = overlap / sum(reference.values())
        score = (
            0.0
            if precision + recall == 0
            else 2 * precision * recall / (precision + recall)
        )

        passed = score >= self._threshold
        return EvalResult(
            name=self.name,
            value=round(score, 4),
            passed=passed,
            evidence="" if passed else f"Token F1 {score:.3f} < 阈值 {self._threshold}",
        )


@register_evaluator("edit_distance")
class EditDistanceEvaluator(BaseEvaluator):
    """归一化编辑距离相似度（1 - 距离/最大长度）。

    用于结构化输出的微小偏差检测 —— 精确匹配太严，语义相似度太松。
    """

    kind: ClassVar[EvaluatorKind] = EvaluatorKind.STATISTICAL

    def __init__(
        self, name: str = "edit_distance", *, threshold: float = 0.9
    ) -> None:
        super().__init__(name)
        self._threshold = threshold

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        if ctx.expected is None:
            return EvalResult(
                name=self.name,
                value=0.0,
                passed=False,
                evidence="edit_distance 需要 expected",
                errored=True,
            )

        similarity = _normalized_similarity(
            ctx.output[:MAX_TEXT_CHARS], ctx.expected[:MAX_TEXT_CHARS]
        )
        passed = similarity >= self._threshold
        return EvalResult(
            name=self.name,
            value=round(similarity, 4),
            passed=passed,
            evidence=""
            if passed
            else f"相似度 {similarity:.3f} < 阈值 {self._threshold}",
        )


def _normalized_similarity(a: str, b: str) -> float:
    """优先用 rapidfuzz（C 实现），缺失时退化到标准库。

    退化路径存在的理由：rapidfuzz 是可选依赖，缺它不该让整个评测不可用。
    """
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0

    try:
        from rapidfuzz.distance import Levenshtein

        return Levenshtein.normalized_similarity(a, b)
    except ImportError:
        from difflib import SequenceMatcher

        return SequenceMatcher(None, a, b).ratio()
