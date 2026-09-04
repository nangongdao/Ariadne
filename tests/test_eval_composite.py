"""复合评分测试。

两个高风险点：
1. 量纲不统一时加权求和无意义 → 验证归一化
2. errored 项被当 0 分会让 Loop 朝错方向修 → 验证它不计入分母
"""

from __future__ import annotations

import math

import pytest

from ariadne.eval_module import EvaluatorFactory
from ariadne.eval_module.base import (
    BaseEvaluator,
    EvalContext,
    EvalResult,
    EvaluatorKind,
    ThresholdOp,
)
from ariadne.eval_module.composite import (
    CompositeScorer,
    EvalSuite,
    ScoreSpec,
    normalize,
)


class Fixed(BaseEvaluator):
    """返回固定值的桩评估器。"""

    kind = EvaluatorKind.DETERMINISTIC

    def __init__(
        self,
        name: str,
        value: float,
        *,
        passed: bool = True,
        errored: bool = False,
        high: float = 1.0,
    ) -> None:
        super().__init__(name)
        self._value = value
        self._passed = passed
        self._errored = errored
        self._high = high

    @property
    def value_range(self) -> tuple[float, float]:
        return (0.0, self._high)

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        return EvalResult(
            name=self.name,
            value=self._value,
            passed=self._passed,
            errored=self._errored,
        )


def ctx() -> EvalContext:
    return EvalContext(item_id="i1", output="x")


class TestNormalize:
    def test_maps_to_unit(self) -> None:
        assert normalize(5.0, 0.0, 10.0) == 0.5

    def test_clamps(self) -> None:
        assert normalize(20.0, 0.0, 10.0) == 1.0
        assert normalize(-5.0, 0.0, 10.0) == 0.0

    def test_infinite_range_raises(self) -> None:
        """静默当成 1.0 会让权重失效，是难查的 bug —— 必须显式报错。"""
        with pytest.raises(ValueError, match="normalize_max"):
            normalize(5.0, 0.0, math.inf)

    def test_nan_becomes_zero(self) -> None:
        assert normalize(math.nan, 0.0, 1.0) == 0.0

    def test_degenerate_range(self) -> None:
        assert normalize(1.0, 1.0, 1.0) == 1.0


class TestWeighting:
    def test_equal_weights_average(self) -> None:
        scorer = CompositeScorer(
            (
                ScoreSpec(Fixed("a", 1.0)),
                ScoreSpec(Fixed("b", 0.0)),
            ),
            threshold=0.0,
        )
        assert scorer.evaluate(ctx()).score == 50.0

    def test_weights_need_not_sum_to_one(self) -> None:
        """内部按总权重归一化，增删项时不必重算所有权重。"""
        scorer = CompositeScorer(
            (
                ScoreSpec(Fixed("a", 1.0), weight=3.0),
                ScoreSpec(Fixed("b", 0.0), weight=1.0),
            ),
            threshold=0.0,
        )
        assert scorer.evaluate(ctx()).score == 75.0

    def test_different_scales_normalized(self) -> None:
        """关键：字数(0-1000) 与相似度(0-1) 混合时必须先归一化。"""
        scorer = CompositeScorer(
            (
                ScoreSpec(Fixed("sim", 1.0, high=1.0)),
                ScoreSpec(Fixed("words", 500.0, high=1000.0)),
            ),
            threshold=0.0,
        )
        # 归一化后是 (1.0 + 0.5) / 2 = 0.75
        assert scorer.evaluate(ctx()).score == 75.0

    def test_negative_weight_rejected(self) -> None:
        with pytest.raises(ValueError, match="权重不能为负"):
            CompositeScorer((ScoreSpec(Fixed("a", 1.0), weight=-1.0),))

    def test_empty_specs_rejected(self) -> None:
        with pytest.raises(ValueError, match="至少需要一个"):
            CompositeScorer(())

    def test_zero_total_weight_rejected(self) -> None:
        with pytest.raises(ValueError, match="权重之和"):
            CompositeScorer((ScoreSpec(Fixed("a", 1.0), weight=0.0),))


class TestErroredHandling:
    def test_errored_excluded_from_denominator(self) -> None:
        """出错项既不加分也不扣分 —— 否则"没测出来"被当"很差"。"""
        scorer = CompositeScorer(
            (
                ScoreSpec(Fixed("ok", 1.0)),
                ScoreSpec(Fixed("broken", 0.0, errored=True)),
            ),
            threshold=0.0,
        )
        result = scorer.evaluate(ctx())
        # 只有 ok 计入，得满分而非 50
        assert result.score == 100.0
        assert result.errored_names == ("broken",)

    def test_all_errored_never_passes(self) -> None:
        """全部出错时判通过就是掩盖故障。"""
        scorer = CompositeScorer(
            (ScoreSpec(Fixed("a", 0.0, errored=True)),), threshold=0.0
        )
        result = scorer.evaluate(ctx())
        assert not result.passed
        assert result.score == 0.0

    def test_missing_normalize_max_degrades_to_binary(self) -> None:
        """无穷上界且未给 normalize_max：退化为二值并记为异常项。"""
        scorer = CompositeScorer(
            (ScoreSpec(Fixed("unbounded", 42.0, high=math.inf)),), threshold=0.0
        )
        result = scorer.evaluate(ctx())
        assert "unbounded" in result.errored_names

    def test_normalize_max_override_works(self) -> None:
        scorer = CompositeScorer(
            (
                ScoreSpec(
                    Fixed("count", 50.0, high=math.inf), normalize_max=100.0
                ),
            ),
            threshold=0.0,
        )
        result = scorer.evaluate(ctx())
        assert result.score == 50.0
        assert not result.errored_names


class TestPassing:
    def test_threshold_enforced(self) -> None:
        scorer = CompositeScorer((ScoreSpec(Fixed("a", 0.8)),), threshold=85.0)
        assert not scorer.evaluate(ctx()).passed

        scorer2 = CompositeScorer((ScoreSpec(Fixed("a", 0.9)),), threshold=85.0)
        assert scorer2.evaluate(ctx()).passed

    def test_individual_failure_blocks_pass(self) -> None:
        """单项不通过时即使复合分够也不算通过 —— 高分不能掩盖硬性失败。"""
        scorer = CompositeScorer(
            (
                ScoreSpec(Fixed("high", 1.0)),
                ScoreSpec(Fixed("failing", 1.0, passed=False)),
            ),
            threshold=50.0,
        )
        result = scorer.evaluate(ctx())
        assert result.score == 100.0
        assert not result.passed
        assert result.failed_names == ("failing",)


class TestSpecThreshold:
    """ScoreSpec.threshold / op 曾经只被写、从不被读。

    eval_worker.build_scorer_from_config 逐条填 threshold=sc.get("threshold")，
    而 CompositeScorer 只看 result.passed —— 配置里的每项达标线全部无效，
    实际按评估器构造器的默认线判定。R12 的字段版。
    """

    def test_threshold_can_tighten(self) -> None:
        """评估器自己说通过，但没到 spec 要求的线 → 判不通过。"""
        spec = ScoreSpec(Fixed("a", 0.6, passed=True), threshold=0.8)
        result = CompositeScorer((spec,), threshold=0.0).evaluate(ctx())
        assert result.failed_names == ("a",)
        assert not result.passed

    def test_threshold_can_loosen(self) -> None:
        """覆盖而非取交集：放宽是这个字段存在的理由。

        评估器的默认线是通用值（如 rouge_l 的 0.5），实验想要自己的线；
        取交集会让 threshold 只能收紧，配了也调不松。
        """
        spec = ScoreSpec(Fixed("a", 0.6, passed=False), threshold=0.5)
        result = CompositeScorer((spec,), threshold=0.0).evaluate(ctx())
        assert result.failed_names == ()
        assert result.passed

    def test_op_applies(self) -> None:
        """op 决定比较方向 —— 越小越好的指标（延迟、编辑距离）要 LTE。"""
        spec = ScoreSpec(Fixed("a", 0.3), threshold=0.5, op=ThresholdOp.LTE)
        assert CompositeScorer((spec,), threshold=0.0).evaluate(ctx()).passed

        spec2 = ScoreSpec(Fixed("b", 0.7), threshold=0.5, op=ThresholdOp.LTE)
        assert not CompositeScorer((spec2,), threshold=0.0).evaluate(ctx()).passed

    def test_no_threshold_keeps_evaluator_verdict(self) -> None:
        spec = ScoreSpec(Fixed("a", 0.1, passed=True))
        assert CompositeScorer((spec,), threshold=0.0).evaluate(ctx()).passed

    def test_errored_result_is_not_thresholded(self) -> None:
        """出错时 value 无意义（通常是 0.0），套阈值会把"没测出来"变成"没达标"。"""
        spec = ScoreSpec(Fixed("a", 0.0, errored=True), threshold=0.5)
        result = CompositeScorer((spec,), threshold=0.0).evaluate(ctx())
        assert result.errored_names == ("a",)
        # 全部项 errored → 不能判通过，但原因是"没测出来"而非"没达标"
        assert not result.passed

    def test_score_still_uses_raw_value(self) -> None:
        """阈值只改 passed，不改分数 —— 分数是连续量，用于趋势与对比。"""
        spec = ScoreSpec(Fixed("a", 0.6, passed=True), threshold=0.9)
        result = CompositeScorer((spec,), threshold=0.0).evaluate(ctx())
        assert result.score == 60.0
        assert not result.passed


class TestAggregation:
    def test_contributions_recorded(self) -> None:
        scorer = CompositeScorer(
            (
                ScoreSpec(Fixed("a", 1.0), weight=2.0),
                ScoreSpec(Fixed("b", 0.5), weight=1.0),
            ),
            threshold=0.0,
        )
        contributions = scorer.evaluate(ctx()).contributions
        assert contributions["a"] == 2.0
        assert contributions["b"] == 0.5

    def test_duration_summed(self) -> None:
        scorer = CompositeScorer(
            (ScoreSpec(Fixed("a", 1.0)), ScoreSpec(Fixed("b", 1.0))), threshold=0.0
        )
        assert scorer.evaluate(ctx()).total_duration_ms >= 0


class TestEvalSuite:
    def test_all_passed(self) -> None:
        suite = EvalSuite((Fixed("a", 1.0), Fixed("b", 1.0)))
        assert suite.all_passed(ctx())

    def test_one_failure_fails_suite(self) -> None:
        """断言集是"全部必须通过"，与加权平均达标是不同语义。"""
        suite = EvalSuite((Fixed("a", 1.0), Fixed("b", 0.0, passed=False)))
        assert not suite.all_passed(ctx())


class TestRealEvaluators:
    def test_with_actual_evaluators(self) -> None:
        """用真实评估器组合，验证 value_range 的接线正确。"""
        scorer = CompositeScorer(
            (
                ScoreSpec(
                    EvaluatorFactory("citation_count", min_count=2),
                    normalize_max=5.0,
                ),
                ScoreSpec(EvaluatorFactory("markdown_structure")),
            ),
            threshold=50.0,
        )
        doc = "# 标题\n\n见 [a](https://a.com) 与 [b](https://b.com)\n"
        result = scorer.evaluate(
            EvalContext(item_id="i1", output=doc)
        )
        assert result.passed
        assert not result.errored_names
