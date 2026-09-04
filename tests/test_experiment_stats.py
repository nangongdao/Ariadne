"""对比统计与门禁测试。

核心风险点：
1. 只看均值会漏掉"一半变好一半变差" → 验证 churn_summary
2. 门禁配了指标但没测到 → 必须报错而非静默放过
3. bootstrap 必须确定可复现 → 否则 CI 门禁会随机抖动
"""

from __future__ import annotations

import pytest

from ariadne.experiment import (
    DEFAULT_RULES,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_REGRESSED,
    Direction,
    GateKind,
    GateRule,
    bootstrap_ci,
    churn_summary,
    compare_metric,
    diff_samples,
    evaluate_gate,
    parse_rules,
    variance_warning,
)


class TestBootstrap:
    def test_deterministic(self) -> None:
        """同样输入必须给同样区间，否则 CI 门禁会随机抖动。"""
        deltas = [0.1, -0.2, 0.3, 0.05, -0.1] * 10
        assert bootstrap_ci(deltas) == bootstrap_ci(deltas)

    def test_all_positive_deltas_ci_above_zero(self) -> None:
        low, high = bootstrap_ci([0.5] * 50)
        assert low > 0 and high > 0

    def test_empty_returns_zero(self) -> None:
        assert bootstrap_ci([]) == (0.0, 0.0)

    def test_single_sample(self) -> None:
        assert bootstrap_ci([0.3]) == (0.3, 0.3)

    def test_mixed_deltas_ci_spans_zero(self) -> None:
        """有正有负且均值接近 0 时，区间应跨过 0（差异不显著）。"""
        low, high = bootstrap_ci([0.5, -0.5] * 50)
        assert low < 0 < high


class TestCompareMetric:
    def test_improvement_detected(self) -> None:
        baseline = [70.0] * 50
        current = [85.0] * 50
        stat = compare_metric("quality", baseline, current)
        assert stat.direction is Direction.IMPROVED
        assert stat.significant
        assert stat.delta == 15.0

    def test_regression_detected(self) -> None:
        stat = compare_metric("quality", [85.0] * 50, [70.0] * 50)
        assert stat.direction is Direction.REGRESSED

    def test_lower_is_better_inverts(self) -> None:
        """成本类指标下降才是改善。"""
        stat = compare_metric(
            "cost", [0.10] * 50, [0.05] * 50, higher_is_better=False
        )
        assert stat.direction is Direction.IMPROVED

    def test_small_sample_inconclusive(self) -> None:
        """样本太少时 bootstrap 结论不可靠。"""
        stat = compare_metric("quality", [70.0] * 5, [90.0] * 5)
        assert stat.direction is Direction.INCONCLUSIVE

    def test_tiny_effect_is_unchanged(self) -> None:
        """0.1% 的变化不该被当成改善 —— 会让门禁充满噪声。"""
        baseline = [80.0] * 50
        current = [80.04] * 50
        stat = compare_metric("quality", baseline, current, min_effect_pct=1.0)
        assert stat.direction is Direction.UNCHANGED

    def test_noisy_data_not_significant(self) -> None:
        baseline = [50.0, 90.0] * 25
        current = [90.0, 50.0] * 25
        stat = compare_metric("quality", baseline, current)
        assert not stat.significant
        assert stat.direction is Direction.UNCHANGED

    def test_uneven_lengths_truncated(self) -> None:
        stat = compare_metric("quality", [80.0] * 50, [90.0] * 30)
        assert stat.sample_count == 30

    def test_empty_inputs(self) -> None:
        stat = compare_metric("quality", [], [])
        assert stat.direction is Direction.INCONCLUSIVE
        assert stat.sample_count == 0

    def test_zero_baseline_treated_as_full_swing(self) -> None:
        """基线为 0 时百分比无定义。返回 0 会让 0→1 被判成"无变化"，
        而那是最大幅度的改善 —— 通过率从 0% 涨到 100% 绝不是噪声。"""
        stat = compare_metric("quality", [0.0] * 50, [5.0] * 50)
        assert stat.delta_pct == 100.0
        assert stat.direction is Direction.IMPROVED

    def test_zero_baseline_regression(self) -> None:
        stat = compare_metric(
            "cost", [0.0] * 50, [5.0] * 50, higher_is_better=False
        )
        assert stat.delta_pct == 100.0
        assert stat.direction is Direction.REGRESSED

    def test_both_zero_is_unchanged(self) -> None:
        stat = compare_metric("quality", [0.0] * 50, [0.0] * 50)
        assert stat.delta_pct == 0.0
        assert stat.direction is Direction.UNCHANGED

    def test_pass_rate_zero_to_one_not_filtered_by_min_effect(self) -> None:
        """回归测试：min_effect_pct 不能吃掉满幅变化。"""
        stat = compare_metric(
            "assertion_pass_rate", [0.0] * 50, [1.0] * 50, min_effect_pct=5.0
        )
        assert stat.direction is Direction.IMPROVED

    def test_describe_readable(self) -> None:
        stat = compare_metric("quality", [70.0] * 50, [85.0] * 50)
        text = stat.describe()
        assert "quality" in text and "↑" in text and "CI[" in text


class TestSampleDiff:
    def test_sorted_by_magnitude(self) -> None:
        diffs = diff_samples(
            {"a": 1.0, "b": 1.0, "c": 1.0}, {"a": 2.0, "b": 5.0, "c": 1.5}
        )
        assert [d.item_id for d in diffs] == ["b", "a", "c"]

    def test_only_common_items(self) -> None:
        diffs = diff_samples({"a": 1.0, "only_base": 1.0}, {"a": 2.0, "only_cur": 1.0})
        assert [d.item_id for d in diffs] == ["a"]

    def test_flip_detection(self) -> None:
        diffs = diff_samples({"pass_to_fail": 1.0, "fail_to_pass": 0.0},
                            {"pass_to_fail": 0.0, "fail_to_pass": 1.0})
        by_id = {d.item_id: d for d in diffs}
        assert by_id["pass_to_fail"].flipped_to_fail
        assert by_id["fail_to_pass"].flipped_to_pass

    def test_min_delta_filter(self) -> None:
        diffs = diff_samples(
            {"tiny": 1.0, "big": 1.0}, {"tiny": 1.001, "big": 2.0}, min_delta=0.01
        )
        assert [d.item_id for d in diffs] == ["big"]

    def test_churn_reveals_hidden_regression(self) -> None:
        """关键场景：均值持平但一半变好一半变差 —— 只看均值会完全漏掉。"""
        baseline = {f"i{n}": 50.0 for n in range(20)}
        current = {f"i{n}": (90.0 if n < 10 else 10.0) for n in range(20)}

        stat = compare_metric(
            "quality", list(baseline.values()), list(current.values())
        )
        assert stat.direction is Direction.UNCHANGED, "均值确实持平"

        churn = churn_summary(diff_samples(baseline, current))
        assert churn["improved"] == 10
        assert churn["regressed"] == 10, "样本级 diff 暴露了隐藏的退化"


class TestVarianceWarning:
    def test_stable_data_no_warning(self) -> None:
        assert variance_warning([80.0, 81.0, 79.0, 80.5]) == ""

    def test_unstable_data_warns(self) -> None:
        warning = variance_warning([10.0, 90.0, 20.0, 95.0])
        assert "变异系数" in warning
        assert "temperature" in warning

    def test_single_value_no_warning(self) -> None:
        assert variance_warning([80.0]) == ""

    def test_zero_mean_no_crash(self) -> None:
        assert variance_warning([-5.0, 5.0]) == ""


class TestGate:
    @staticmethod
    def stat_for(metric: str, base: float, cur: float, n: int = 50):
        higher_better = metric != "cost_per_item"
        return compare_metric(
            metric, [base] * n, [cur] * n, higher_is_better=higher_better
        )

    def test_pass_when_improved(self) -> None:
        stats = [
            self.stat_for("composite_quality", 80.0, 88.0),
            self.stat_for("assertion_pass_rate", 0.90, 0.95),
            self.stat_for("cost_per_item", 0.05, 0.04),
        ]
        report = evaluate_gate(DEFAULT_RULES, stats)
        assert report.passed
        assert report.exit_code == EXIT_OK

    def test_quality_regression_blocks(self) -> None:
        stats = [
            self.stat_for("composite_quality", 80.0, 75.0),  # -6.25%
            self.stat_for("assertion_pass_rate", 0.90, 0.90),
            self.stat_for("cost_per_item", 0.05, 0.05),
        ]
        report = evaluate_gate(DEFAULT_RULES, stats)
        assert not report.passed
        assert report.exit_code == EXIT_REGRESSED
        assert "composite_quality" in report.describe()

    def test_cost_increase_blocks_even_if_quality_up(self) -> None:
        """质量提升但成本翻倍 —— 只看质量的门禁会放过这种退化。"""
        stats = [
            self.stat_for("composite_quality", 80.0, 82.0),
            self.stat_for("assertion_pass_rate", 0.90, 0.90),
            self.stat_for("cost_per_item", 0.05, 0.10),  # +100%
        ]
        report = evaluate_gate(DEFAULT_RULES, stats)
        assert not report.passed
        assert any("cost_per_item" in v.describe() for v in report.violations)

    def test_pass_rate_zero_tolerance(self) -> None:
        """断言通过率不允许任何下降，且不要求显著性。"""
        stats = [
            self.stat_for("composite_quality", 80.0, 80.0),
            self.stat_for("assertion_pass_rate", 0.95, 0.94),
            self.stat_for("cost_per_item", 0.05, 0.05),
        ]
        report = evaluate_gate(DEFAULT_RULES, stats)
        assert not report.passed

    def test_missing_metric_is_error_not_pass(self) -> None:
        """门禁配了却没测到，通常是评估器名写错 —— 静默放过会让门禁形同虚设。"""
        report = evaluate_gate(DEFAULT_RULES, [])
        assert not report.passed
        assert report.exit_code == EXIT_ERROR
        assert "未在实验结果中找到" in report.describe()

    def test_inconclusive_not_counted_as_violation(self) -> None:
        stats = [self.stat_for("composite_quality", 80.0, 60.0, n=5)]
        rules = (
            GateRule(
                metric="composite_quality", kind=GateKind.DEGRADATION, max_pct=1.0
            ),
        )
        report = evaluate_gate(rules, stats)
        assert not report.violations
        assert "composite_quality" in report.inconclusive
        assert "未参与判定" in report.describe()

    def test_require_significant_suppresses_noise(self) -> None:
        noisy_base = [50.0, 90.0] * 25
        noisy_cur = [90.0, 45.0] * 25
        stat = compare_metric("composite_quality", noisy_base, noisy_cur)
        rules = (
            GateRule(
                metric="composite_quality",
                kind=GateKind.DEGRADATION,
                max_pct=0.0,
                require_significant=True,
            ),
        )
        assert evaluate_gate(rules, [stat]).passed


class TestParseRules:
    def test_degradation_and_increase(self) -> None:
        rules = parse_rules([
            {"metric": "quality", "degradation_pct": 3},
            {"metric": "cost", "increase_pct": 20},
        ])
        assert rules[0].kind is GateKind.DEGRADATION
        assert rules[1].kind is GateKind.INCREASE

    def test_missing_metric_raises(self) -> None:
        with pytest.raises(ValueError, match="缺少 metric"):
            parse_rules([{"degradation_pct": 3}])

    def test_missing_direction_raises(self) -> None:
        with pytest.raises(ValueError, match="degradation_pct 或 increase_pct"):
            parse_rules([{"metric": "quality"}])

    def test_require_significant_override(self) -> None:
        rules = parse_rules([
            {"metric": "q", "degradation_pct": 0, "require_significant": False}
        ])
        assert not rules[0].require_significant
