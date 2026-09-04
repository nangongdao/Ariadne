"""统计类评估器测试。

重点：这类指标适合回归检测而非质量评判 —— 测试要显式验证
"语义对但表述不同会得低分"这个已知局限，避免后来者误用。
"""

from __future__ import annotations

import pytest

from ariadne.eval_module import EvaluatorFactory
from ariadne.eval_module.base import EvalContext
from ariadne.eval_module.statistical import tokenize
from ariadne.eval_module.statistical.overlap import _lcs_length


def ctx(output: str, expected: str | None = None) -> EvalContext:
    return EvalContext(item_id="i1", output=output, expected=expected)


class TestTokenize:
    def test_mixed_script(self) -> None:
        assert tokenize("Hello 世界 123") == ["hello", "世界"[0], "世界"[1], "123"]

    def test_lowercases(self) -> None:
        assert tokenize("ABC") == ["abc"]

    def test_drops_punctuation(self) -> None:
        assert tokenize("a, b. c!") == ["a", "b", "c"]


class TestLcs:
    def test_identical(self) -> None:
        assert _lcs_length(["a", "b", "c"], ["a", "b", "c"]) == 3

    def test_subsequence(self) -> None:
        assert _lcs_length(["a", "b", "c"], ["a", "c"]) == 2

    def test_disjoint(self) -> None:
        assert _lcs_length(["a"], ["b"]) == 0

    def test_empty(self) -> None:
        assert _lcs_length([], ["a"]) == 0

    def test_long_input_does_not_blow_memory(self) -> None:
        """滚动数组实现：2000×2000 完整 DP 表是 400 万格，滚动只需两行。"""
        a = [str(i % 50) for i in range(2000)]
        b = [str(i % 50) for i in range(2000)]
        assert _lcs_length(a, b) > 0


class TestRougeL:
    def test_identical_scores_one(self) -> None:
        ev = EvaluatorFactory("rouge_l", threshold=0.9)
        result = ev.evaluate(ctx("the quick brown fox", "the quick brown fox"))
        assert result.value == 1.0
        assert result.passed

    def test_word_order_matters(self) -> None:
        """ROUGE-L 基于 LCS，对语序敏感 —— 这是它与 token_f1 的分工。"""
        ev = EvaluatorFactory("rouge_l", threshold=0.99)
        scrambled = ev.evaluate(ctx("fox brown quick the", "the quick brown fox"))
        assert scrambled.value < 1.0

    def test_missing_expected_flags_errored(self) -> None:
        ev = EvaluatorFactory("rouge_l")
        result = ev.evaluate(ctx("output"))
        assert result.errored
        assert not result.passed

    def test_empty_output(self) -> None:
        ev = EvaluatorFactory("rouge_l")
        result = ev.evaluate(ctx("", "reference"))
        assert result.value == 0.0
        assert not result.passed

    def test_evidence_includes_components(self) -> None:
        ev = EvaluatorFactory("rouge_l", threshold=0.99)
        result = ev.evaluate(ctx("a b", "a b c d e f"))
        assert "P=" in result.evidence
        assert "LCS=" in result.evidence


class TestTokenF1:
    def test_order_insensitive(self) -> None:
        """与 ROUGE-L 互补：内容对但顺序乱时此指标不受影响。"""
        ev = EvaluatorFactory("token_f1", threshold=0.99)
        result = ev.evaluate(ctx("fox brown quick the", "the quick brown fox"))
        assert result.value == 1.0
        assert result.passed

    def test_repeated_words_do_not_inflate(self) -> None:
        """多重集交集：刷词不能提分。"""
        ev = EvaluatorFactory("token_f1", threshold=0.0)
        padded = ev.evaluate(ctx("cat cat cat cat", "cat dog"))
        assert padded.value < 1.0

    def test_complements_rouge_on_reordering(self) -> None:
        """同一对文本上，乱序时 token_f1 应高于 ROUGE-L。"""
        pair = ("d c b a", "a b c d")
        rouge = EvaluatorFactory("rouge_l", threshold=0.0).evaluate(ctx(*pair))
        f1 = EvaluatorFactory("token_f1", threshold=0.0).evaluate(ctx(*pair))
        assert f1.value > rouge.value


class TestEditDistance:
    def test_identical(self) -> None:
        ev = EvaluatorFactory("edit_distance", threshold=0.9)
        assert ev.evaluate(ctx("abc", "abc")).value == 1.0

    def test_small_typo_still_high(self) -> None:
        """精确匹配太严、语义相似度太松时用它 —— 单字符差异应仍算高相似。"""
        ev = EvaluatorFactory("edit_distance", threshold=0.9)
        result = ev.evaluate(ctx("hello world", "hello worle"))
        assert result.value > 0.9
        assert result.passed

    def test_completely_different(self) -> None:
        ev = EvaluatorFactory("edit_distance", threshold=0.5)
        assert not ev.evaluate(ctx("aaaa", "zzzz")).passed

    def test_both_empty_is_identical(self) -> None:
        ev = EvaluatorFactory("edit_distance")
        assert ev.evaluate(ctx("", "")).value == 1.0


class TestKnownLimitation:
    """显式记录已知局限，避免后来者把统计指标当质量评判用。"""

    def test_paraphrase_gets_low_score(self) -> None:
        """语义等价但用词不同 → 低分。这是为什么这类指标不能替代 Judge。"""
        paraphrase = "该函数返回两数之和"
        reference = "此方法计算并给出两个数字相加的结果"

        rouge = EvaluatorFactory("rouge_l", threshold=0.0).evaluate(
            ctx(paraphrase, reference)
        )
        # 语义几乎相同，但字面重合度低
        assert rouge.value < 0.6, (
            "若此断言失败说明分词或指标变了，需重新评估该局限的表述"
        )


@pytest.mark.parametrize(
    "evaluator_name", ["rouge_l", "token_f1", "edit_distance"]
)
def test_no_expected_never_raises(evaluator_name: str) -> None:
    """契约：缺 expected 要返回 errored 而非抛异常。"""
    ev = EvaluatorFactory(evaluator_name)
    result = ev.evaluate(ctx("output only"))
    assert result.errored
    assert not result.passed
