"""对比统计。

用 bootstrap 置信区间而非只报均值差：均值持平可能掩盖"一半变好一半变差"，
而置信区间能说明差异是否可信。

自己实现 bootstrap 而非依赖 scipy：算法本身十几行，且 scipy 是重依赖
（~30MB）。真正需要 scipy 的高级检验留到有明确需求时再引入。
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from statistics import mean, stdev

DEFAULT_BOOTSTRAP_ROUNDS = 2000
DEFAULT_CONFIDENCE = 0.95
# 样本量低于此值时，bootstrap 的结论不可靠
MIN_SAMPLES_FOR_INFERENCE = 10


class Direction(StrEnum):
    IMPROVED = "improved"
    REGRESSED = "regressed"
    UNCHANGED = "unchanged"
    INCONCLUSIVE = "inconclusive"  # 样本太少或方差太大


@dataclass(frozen=True)
class ComparisonStat:
    """单个指标的对比结果。"""

    metric: str
    baseline_mean: float
    current_mean: float
    delta: float
    delta_pct: float
    ci_low: float
    ci_high: float
    direction: Direction
    sample_count: int
    # 置信区间是否跨过 0 —— 跨过说明差异不显著
    significant: bool

    def describe(self) -> str:
        arrow = {
            Direction.IMPROVED: "↑",
            Direction.REGRESSED: "↓",
            Direction.UNCHANGED: "=",
            Direction.INCONCLUSIVE: "?",
        }[self.direction]
        return (
            f"{self.metric}: {self.baseline_mean:.3f} → {self.current_mean:.3f} "
            f"{arrow} {self.delta:+.3f} ({self.delta_pct:+.1f}%) "
            f"CI[{self.ci_low:+.3f}, {self.ci_high:+.3f}]"
            f"{'' if self.significant else ' 不显著'}"
        )


def bootstrap_ci(
    paired_deltas: Sequence[float],
    *,
    rounds: int = DEFAULT_BOOTSTRAP_ROUNDS,
    confidence: float = DEFAULT_CONFIDENCE,
    seed: int = 42,
) -> tuple[float, float]:
    """配对 bootstrap 置信区间。

    用**配对**差值而非独立两样本：同一数据集上的两次实验是配对的，
    配对检验的功效显著高于独立检验（消除了样本本身难度的方差）。

    seed 固定：同样的输入必须给出同样的区间，否则 CI 门禁会随机抖动。
    """
    if not paired_deltas:
        return (0.0, 0.0)
    if len(paired_deltas) == 1:
        only = paired_deltas[0]
        return (only, only)

    rng = random.Random(seed)
    n = len(paired_deltas)
    means: list[float] = []
    for _ in range(rounds):
        resample = [paired_deltas[rng.randrange(n)] for _ in range(n)]
        means.append(mean(resample))

    means.sort()
    alpha = (1.0 - confidence) / 2.0
    low_index = int(alpha * rounds)
    high_index = min(int((1.0 - alpha) * rounds), rounds - 1)
    return (round(means[low_index], 6), round(means[high_index], 6))


def compare_metric(
    metric: str,
    baseline: Sequence[float],
    current: Sequence[float],
    *,
    higher_is_better: bool = True,
    min_effect_pct: float = 1.0,
) -> ComparisonStat:
    """对比单个指标。

    baseline 与 current 必须是**同顺序的配对样本**（同一数据集同一样本）。
    长度不等时截断到较短的，并在样本量不足时标 INCONCLUSIVE。
    """
    paired_n = min(len(baseline), len(current))
    if paired_n == 0:
        return ComparisonStat(
            metric=metric,
            baseline_mean=0.0,
            current_mean=0.0,
            delta=0.0,
            delta_pct=0.0,
            ci_low=0.0,
            ci_high=0.0,
            direction=Direction.INCONCLUSIVE,
            sample_count=0,
            significant=False,
        )

    base = list(baseline[:paired_n])
    curr = list(current[:paired_n])
    deltas = [c - b for b, c in zip(base, curr, strict=True)]

    base_mean = mean(base)
    curr_mean = mean(curr)
    delta = curr_mean - base_mean

    # 基线为 0 时百分比变化数学上无定义。返回 0 会让 0→1 这种
    # 最大幅度的变化被 min_effect_pct 判成"无变化"，因此按满幅计。
    baseline_is_zero = base_mean == 0
    if baseline_is_zero:
        delta_pct = 0.0 if delta == 0 else (100.0 if delta > 0 else -100.0)
    else:
        delta_pct = delta / abs(base_mean) * 100.0

    ci_low, ci_high = bootstrap_ci(deltas)
    # 区间跨 0 说明无法排除"没有差异"
    significant = (ci_low > 0 and ci_high > 0) or (ci_low < 0 and ci_high < 0)

    direction = _classify(
        delta=delta,
        delta_pct=delta_pct,
        significant=significant,
        higher_is_better=higher_is_better,
        # 基线为 0 且有变化时跳过最小效应过滤：这是满幅变化，不是噪声
        min_effect_pct=0.0 if baseline_is_zero and delta != 0 else min_effect_pct,
        sample_count=paired_n,
    )

    return ComparisonStat(
        metric=metric,
        baseline_mean=round(base_mean, 6),
        current_mean=round(curr_mean, 6),
        delta=round(delta, 6),
        delta_pct=round(delta_pct, 4),
        ci_low=ci_low,
        ci_high=ci_high,
        direction=direction,
        sample_count=paired_n,
        significant=significant,
    )


def _classify(
    *,
    delta: float,
    delta_pct: float,
    significant: bool,
    higher_is_better: bool,
    min_effect_pct: float,
    sample_count: int,
) -> Direction:
    if sample_count < MIN_SAMPLES_FOR_INFERENCE:
        return Direction.INCONCLUSIVE
    if not significant or abs(delta_pct) < min_effect_pct:
        return Direction.UNCHANGED

    improved = delta > 0 if higher_is_better else delta < 0
    return Direction.IMPROVED if improved else Direction.REGRESSED


@dataclass(frozen=True)
class SampleDiff:
    """样本级差异。

    这比看均值有用得多 —— 均值持平可能掩盖"一半变好一半变差"，
    而那通常意味着改动引入了新的失败模式。
    """

    item_id: str
    baseline_value: float
    current_value: float
    delta: float

    @property
    def flipped_to_pass(self) -> bool:
        return self.baseline_value <= 0 and self.current_value > 0

    @property
    def flipped_to_fail(self) -> bool:
        return self.baseline_value > 0 and self.current_value <= 0


def diff_samples(
    baseline: dict[str, float],
    current: dict[str, float],
    *,
    min_delta: float = 0.0,
) -> tuple[SampleDiff, ...]:
    """逐样本对比，按变化幅度降序。只返回两侧都有的样本。"""
    diffs = [
        SampleDiff(
            item_id=item_id,
            baseline_value=baseline[item_id],
            current_value=current[item_id],
            delta=round(current[item_id] - baseline[item_id], 6),
        )
        for item_id in baseline.keys() & current.keys()
    ]
    filtered = [d for d in diffs if abs(d.delta) > min_delta]
    return tuple(sorted(filtered, key=lambda d: abs(d.delta), reverse=True))


def churn_summary(diffs: Sequence[SampleDiff]) -> dict[str, int]:
    """变化构成。

    improved 与 regressed 同时很高 → 均值可能持平但实际引入了新失败模式，
    这是"看均值会漏掉"的典型情况。
    """
    improved = sum(1 for d in diffs if d.delta > 0)
    regressed = sum(1 for d in diffs if d.delta < 0)
    return {
        "improved": improved,
        "regressed": regressed,
        "flipped_to_pass": sum(1 for d in diffs if d.flipped_to_pass),
        "flipped_to_fail": sum(1 for d in diffs if d.flipped_to_fail),
        "total_changed": improved + regressed,
    }


def variance_warning(values: Sequence[float], *, threshold: float = 0.3) -> str:
    """方差过大时的警告。

    变异系数（CV = 标准差/均值）过高说明结果不稳定，此时任何均值对比
    都可能是噪声。常见原因：temperature 未设 0、样本难度分布过广。
    """
    if len(values) < 2:
        return ""
    average = mean(values)
    if average == 0:
        return ""
    cv = stdev(values) / abs(average)
    if cv <= threshold:
        return ""
    return (
        f"变异系数 {cv:.2f} 超过 {threshold}，结果不稳定。"
        "检查 temperature 是否为 0、样本难度是否过于分散。"
    )
