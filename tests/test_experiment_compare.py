"""实验对比测试。

最重要的一条：**跨数据集对比必须被拒绝**。在不同数据集上比均值毫无意义，
而这是最容易犯又最难发现的错误。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ariadne.eval_module.base import (
    BaseEvaluator,
    EvalContext,
    EvalResult,
    EvaluatorKind,
)
from ariadne.eval_module.composite import CompositeScorer, ScoreSpec
from ariadne.experiment import (
    METRIC_COMPOSITE,
    METRIC_COST,
    Dataset,
    DatasetItem,
    DatasetMismatchError,
    ExperimentResult,
    ExperimentRunner,
    GenerationOutput,
    compare,
    format_report,
)
from ariadne.experiment.stats import Direction


class ScoreByLength(BaseEvaluator):
    kind = EvaluatorKind.DETERMINISTIC

    @property
    def value_range(self) -> tuple[float, float]:
        return (0.0, 100.0)

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        score = min(len(ctx.output) * 10.0, 100.0)
        return EvalResult(name=self.name, value=score, passed=score >= 50.0)


class MappedGenerator:
    def __init__(self, mapping: dict[str, GenerationOutput]) -> None:
        self._mapping = mapping

    def generate(self, item: DatasetItem) -> GenerationOutput:
        return self._mapping.get(item.item_id, GenerationOutput(text="x" * 5))


def dataset(n: int = 20, *, name: str = "core") -> Dataset:
    return Dataset.create(
        dataset_id="d1",
        name=name,
        version=1,
        items=[DatasetItem(item_id=f"i{k:02d}", input=f"q{k}") for k in range(n)],
    )


def run(
    ds: Dataset, outputs: dict[str, GenerationOutput], label: str
) -> ExperimentResult:
    scorer = CompositeScorer((ScoreSpec(ScoreByLength("length")),), threshold=50.0)
    return ExperimentRunner(scorer=scorer).run(
        experiment_id=label,
        dataset=ds,
        generator=MappedGenerator(outputs),
        config_label=label,
    )


def uniform(ds: Dataset, chars: int, cost: str = "0.01") -> dict[str, GenerationOutput]:
    return {
        item.item_id: GenerationOutput(
            text="x" * chars, cost_usd=Decimal(cost)
        )
        for item in ds
    }


class TestDatasetGuard:
    def test_mismatched_dataset_rejected(self) -> None:
        """在不同数据集上比均值无意义 —— 必须拒绝而非警告。"""
        base = run(dataset(20, name="core"), {}, "base")
        curr = run(dataset(20, name="other"), {}, "curr")
        with pytest.raises(DatasetMismatchError, match="数据集不一致"):
            compare(base, curr)

    def test_explicit_override_allowed(self) -> None:
        base = run(dataset(20, name="core"), {}, "base")
        curr = run(dataset(20, name="other"), {}, "curr")
        report = compare(base, curr, require_same_dataset=False)
        assert report.dataset_ref.startswith("other@")

    def test_same_dataset_passes_guard(self) -> None:
        ds = dataset(20)
        report = compare(run(ds, {}, "base"), run(ds, {}, "curr"))
        assert report.dataset_ref == ds.ref


class TestComparison:
    def test_improvement_passes_gate(self) -> None:
        ds = dataset(20)
        base = run(ds, uniform(ds, 3), "base")     # 30 分
        curr = run(ds, uniform(ds, 10), "curr")    # 100 分
        report = compare(base, curr)

        composite = report.stat_for(METRIC_COMPOSITE)
        assert composite is not None
        assert composite.direction is Direction.IMPROVED
        assert report.passed

    def test_quality_regression_blocks(self) -> None:
        ds = dataset(20)
        base = run(ds, uniform(ds, 10), "base")
        curr = run(ds, uniform(ds, 3), "curr")
        report = compare(base, curr)
        assert not report.passed
        assert report.exit_code == 1

    def test_cost_increase_blocks(self) -> None:
        """质量持平但成本翻倍 —— 只看质量会放过。"""
        ds = dataset(20)
        base = run(ds, uniform(ds, 10, cost="0.01"), "base")
        curr = run(ds, uniform(ds, 10, cost="0.05"), "curr")
        report = compare(base, curr)
        assert not report.passed
        cost_stat = report.stat_for(METRIC_COST)
        assert cost_stat is not None
        assert cost_stat.direction is Direction.REGRESSED


class TestHiddenRegression:
    def test_flat_mean_with_churn_warns(self) -> None:
        """核心场景：均值持平但一半变好一半变差。

        这是"只看均值会漏掉"的典型情况，报告必须显式警告。
        """
        ds = dataset(20)
        base = uniform(ds, 5)  # 全部 50 分
        curr = {
            item.item_id: GenerationOutput(
                text="x" * (10 if index < 10 else 1), cost_usd=Decimal("0.01")
            )
            for index, item in enumerate(ds)
        }
        report = compare(run(ds, base, "base"), run(ds, curr, "curr"))

        assert report.churn["improved"] == 10
        assert report.churn["regressed"] == 10
        assert any("均值持平" in w for w in report.warnings)

    def test_flip_counts_reported(self) -> None:
        ds = dataset(20)
        base = uniform(ds, 10)  # 全通过
        curr = uniform(ds, 1)   # 全失败
        report = compare(run(ds, base, "base"), run(ds, curr, "curr"))
        assert report.churn["flipped_to_fail"] == 20

    def test_top_diffs_limited_and_sorted(self) -> None:
        ds = dataset(20)
        base = uniform(ds, 5)
        curr = {
            item.item_id: GenerationOutput(text="x" * (index % 10 + 1))
            for index, item in enumerate(ds)
        }
        report = compare(run(ds, base, "base"), run(ds, curr, "curr"))
        assert len(report.top_diffs) <= 10
        deltas = [abs(d.delta) for d in report.top_diffs]
        assert deltas == sorted(deltas, reverse=True)


class TestWarnings:
    def test_generation_failures_warned(self) -> None:
        """两侧失败数不同会让"配对"名不副实。"""
        ds = dataset(20)
        base = uniform(ds, 10)
        curr = dict(uniform(ds, 10))
        curr["i00"] = GenerationOutput(text="", error="provider 429")
        report = compare(run(ds, base, "base"), run(ds, curr, "curr"))
        assert any("生成失败样本" in w for w in report.warnings)

    def test_high_variance_warned(self) -> None:
        ds = dataset(20)
        noisy = {
            item.item_id: GenerationOutput(text="x" * (1 if index % 2 else 10))
            for index, item in enumerate(ds)
        }
        report = compare(run(ds, noisy, "base"), run(ds, noisy, "curr"))
        assert any("变异系数" in w for w in report.warnings)


class TestFormatting:
    def test_report_contains_key_sections(self) -> None:
        ds = dataset(20)
        report = compare(run(ds, uniform(ds, 3), "base"), run(ds, uniform(ds, 10), "curr"))
        text = format_report(report)
        assert "数据集" in text
        assert "指标变化" in text
        assert "门禁" in text

    def test_failed_gate_shows_violation(self) -> None:
        ds = dataset(20)
        report = compare(run(ds, uniform(ds, 10), "base"), run(ds, uniform(ds, 3), "curr"))
        text = format_report(report)
        assert "✗" in text
        assert METRIC_COMPOSITE in text

    def test_flip_annotated_in_diff_list(self) -> None:
        ds = dataset(20)
        report = compare(run(ds, uniform(ds, 10), "base"), run(ds, uniform(ds, 1), "curr"))
        assert "转为失败" in format_report(report)


class TestFlipDetectionSeparation:
    """回归测试：翻转检测必须基于通过率（0/1）而非复合分（0-100）。

    复合分 100→10 两边都 >0，据此判断翻转会永远得 0 ——
    这个 bug 在 M2 开发中被测试抓到过。
    """

    def test_score_drop_within_positive_range_still_detects_flip(self) -> None:
        ds = dataset(20)
        # 100 分（通过）→ 10 分（失败）：两个值都 > 0
        base = uniform(ds, 10)
        curr = uniform(ds, 1)
        report = compare(run(ds, base, "base"), run(ds, curr, "curr"))

        assert report.churn["flipped_to_fail"] == 20
        assert len(report.flipped_to_fail) == 20
        # 复合分 diff 仍用于幅度排序
        assert report.top_diffs[0].delta == -90.0

    def test_score_change_without_flip_not_counted(self) -> None:
        """分数变化但仍在通过侧 → 不算翻转。"""
        ds = dataset(20)
        base = uniform(ds, 10)  # 100 分，通过
        curr = uniform(ds, 6)   # 60 分，仍通过（阈值 50）
        report = compare(run(ds, base, "base"), run(ds, curr, "curr"))

        assert report.churn["flipped_to_fail"] == 0
        assert report.churn["regressed"] == 20  # 分数确实降了
