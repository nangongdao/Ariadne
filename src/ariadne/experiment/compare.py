"""实验对比：把两次实验结果变成可读报告与门禁判定。

对比的输出要能同时服务两个用途：
1. 人读（终端报告、前端页面）
2. 机器判定（CI 退出码）

因此 compare() 返回结构化对象，格式化单独一层。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ariadne.experiment.gate import DEFAULT_RULES, GateReport, GateRule, evaluate_gate
from ariadne.experiment.runner import (
    METRIC_COMPOSITE,
    METRIC_COST,
    METRIC_PASS_RATE,
    ExperimentResult,
)
from ariadne.experiment.stats import (
    ComparisonStat,
    Direction,
    SampleDiff,
    churn_summary,
    compare_metric,
    diff_samples,
    variance_warning,
)

# 成本类指标"越低越好"，其余"越高越好"
_LOWER_IS_BETTER = frozenset({METRIC_COST})

TOP_DIFF_COUNT = 10


class DatasetMismatchError(ValueError):
    """两次实验用的不是同一数据集版本。

    必须拒绝而非警告：在不同数据集上比均值毫无意义，
    而这是最容易犯又最难发现的错误。
    """


@dataclass(frozen=True)
class ComparisonReport:
    baseline_label: str
    current_label: str
    dataset_ref: str
    stats: tuple[ComparisonStat, ...]
    gate: GateReport
    top_diffs: tuple[SampleDiff, ...]
    churn: dict[str, int]
    # 从通过转为失败 / 从失败转为通过的 item_id。
    # 单独记录而非从 top_diffs 推断 —— 后者是复合分 diff，
    # 分数 100→10 两边都 >0，无法据此判断是否翻转。
    flipped_to_fail: frozenset[str] = frozenset()
    flipped_to_pass: frozenset[str] = frozenset()
    warnings: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.gate.passed

    @property
    def exit_code(self) -> int:
        return self.gate.exit_code

    def stat_for(self, metric: str) -> ComparisonStat | None:
        return next((s for s in self.stats if s.metric == metric), None)


def compare(
    baseline: ExperimentResult,
    current: ExperimentResult,
    *,
    rules: Sequence[GateRule] = DEFAULT_RULES,
    extra_metrics: Sequence[str] = (),
    require_same_dataset: bool = True,
) -> ComparisonReport:
    """对比两次实验。

    require_same_dataset 默认为 True：在不同数据集上比均值毫无意义。
    刻意跨数据集对比时才显式关掉。
    """
    if require_same_dataset and baseline.dataset_ref != current.dataset_ref:
        raise DatasetMismatchError(
            f"数据集不一致：baseline={baseline.dataset_ref} "
            f"current={current.dataset_ref}。在不同数据集上比均值无意义；"
            "确实要跨数据集对比请设 require_same_dataset=False。"
        )

    metric_names = [
        METRIC_COMPOSITE,
        METRIC_PASS_RATE,
        METRIC_COST,
        *extra_metrics,
    ]

    stats: list[ComparisonStat] = []
    for metric in dict.fromkeys(metric_names):  # 去重且保序
        stats.append(
            compare_metric(
                metric,
                baseline.series(metric),
                current.series(metric),
                higher_is_better=metric not in _LOWER_IS_BETTER,
            )
        )

    gate = evaluate_gate(rules, stats)

    # 两组 diff 回答不同问题，不能合并：
    #   复合分 diff → "哪些样本变化最大"（幅度排序有意义）
    #   通过率 diff → "哪些样本从通过转为失败"（翻转检测需要 0/1 值）
    score_diffs = diff_samples(
        baseline.per_item(METRIC_COMPOSITE), current.per_item(METRIC_COMPOSITE)
    )
    pass_diffs = diff_samples(
        baseline.per_item(METRIC_PASS_RATE), current.per_item(METRIC_PASS_RATE)
    )

    warnings: list[str] = []
    for label, result in (("baseline", baseline), ("current", current)):
        note = variance_warning(result.series(METRIC_COMPOSITE))
        if note:
            warnings.append(f"{label}: {note}")

    # 生成失败会污染对比：失败样本不在均值里，两侧失败数不同会让"配对"名不副实
    base_failures = len(baseline.generation_failures)
    curr_failures = len(current.generation_failures)
    if base_failures or curr_failures:
        warnings.append(
            f"生成失败样本 baseline={base_failures} current={curr_failures}，"
            "这些样本未计入均值，配对可能不完整"
        )

    churn = churn_summary(score_diffs)
    flips = churn_summary(pass_diffs)
    churn["flipped_to_pass"] = flips["flipped_to_pass"]
    churn["flipped_to_fail"] = flips["flipped_to_fail"]

    if churn["improved"] > 0 and churn["regressed"] > 0:
        composite = next(
            (s for s in stats if s.metric == METRIC_COMPOSITE), None
        )
        if composite and composite.direction is Direction.UNCHANGED:
            warnings.append(
                f"均值持平但有 {churn['improved']} 个样本变好、"
                f"{churn['regressed']} 个变差 —— 可能引入了新的失败模式，"
                "建议看样本级 diff 而非只看均值"
            )

    return ComparisonReport(
        baseline_label=baseline.config_label,
        current_label=current.config_label,
        dataset_ref=current.dataset_ref,
        stats=tuple(stats),
        gate=gate,
        top_diffs=score_diffs[:TOP_DIFF_COUNT],
        churn=churn,
        flipped_to_fail=frozenset(
            d.item_id for d in pass_diffs if d.flipped_to_fail
        ),
        flipped_to_pass=frozenset(
            d.item_id for d in pass_diffs if d.flipped_to_pass
        ),
        warnings=tuple(warnings),
    )


def format_report(report: ComparisonReport) -> str:
    """终端报告。供 CLI 与 CI 日志使用。"""
    lines = [
        f"数据集   {report.dataset_ref}",
        f"对比     {report.baseline_label} → {report.current_label}",
        "",
        "指标变化",
    ]
    for stat in report.stats:
        lines.append(f"  {stat.describe()}")

    if report.churn["total_changed"]:
        lines += [
            "",
            "样本变化",
            f"  变好 {report.churn['improved']} · 变差 {report.churn['regressed']}"
            f" · 转为通过 {report.churn['flipped_to_pass']}"
            f" · 转为失败 {report.churn['flipped_to_fail']}",
        ]

    if report.top_diffs:
        lines += ["", f"变化最大的 {len(report.top_diffs)} 个样本"]
        for diff in report.top_diffs:
            flag = ""
            if diff.item_id in report.flipped_to_fail:
                flag = "  ← 转为失败"
            elif diff.item_id in report.flipped_to_pass:
                flag = "  ← 转为通过"
            lines.append(
                f"  {diff.item_id:24} {diff.baseline_value:7.2f} →"
                f" {diff.current_value:7.2f}  {diff.delta:+7.2f}{flag}"
            )

    if report.warnings:
        lines += ["", "警告"]
        lines += [f"  ⚠ {w}" for w in report.warnings]

    lines += ["", "门禁", *(f"  {line}" for line in report.gate.describe().splitlines())]
    return "\n".join(lines)
