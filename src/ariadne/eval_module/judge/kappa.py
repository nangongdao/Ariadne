"""Judge 与人工标注的一致性度量。

用**加权** kappa 而非普通 kappa：Judge 分数是有序等级（差/中/好），
不是名义分类。普通 kappa 把"把好判成中"和"把好判成差"当同等错误，
而前者显然比后者轻 —— 这会低估一个基本可用的 Judge。

κ 门禁（见 docs/05）：
  ≥ 0.8  高度一致，可直接作断言依据
  0.6-0.8 可用，但建议只做 non-blocking 断言
  0.4-0.6 仅作参考指标，禁止用于收敛判定
  < 0.4  不可用
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

MIN_SAMPLES_FOR_KAPPA = 30
RECOMMENDED_SAMPLES = 100

# 门禁阈值
KAPPA_BLOCKING_MIN = 0.6
KAPPA_HIGH_AGREEMENT = 0.8
KAPPA_REFERENCE_ONLY = 0.4


class KappaVerdict(StrEnum):
    HIGH = "high"              # ≥ 0.8
    USABLE = "usable"          # 0.6 - 0.8
    REFERENCE_ONLY = "reference_only"  # 0.4 - 0.6
    UNUSABLE = "unusable"      # < 0.4


@dataclass(frozen=True)
class KappaReport:
    kappa: float
    verdict: KappaVerdict
    sample_count: int
    weighting: Literal["linear", "quadratic", "none"]
    observed_agreement: float
    expected_agreement: float
    # 样本量不足时的警告 —— κ 在小样本上极不稳定
    underpowered: bool

    @property
    def can_block(self) -> bool:
        """是否可作为 blocking 断言的依据。

        样本量不足时一律不允许，即使 κ 数值很高 —— 30 个样本上的
        κ=0.9 不能说明什么。
        """
        return not self.underpowered and self.kappa >= KAPPA_BLOCKING_MIN

    def describe(self) -> str:
        parts = [f"κ={self.kappa:.3f}", f"n={self.sample_count}", self.verdict.value]
        if self.underpowered:
            parts.append(f"样本不足(建议≥{RECOMMENDED_SAMPLES})")
        return " ".join(parts)


def _classify(kappa: float) -> KappaVerdict:
    if kappa >= KAPPA_HIGH_AGREEMENT:
        return KappaVerdict.HIGH
    if kappa >= KAPPA_BLOCKING_MIN:
        return KappaVerdict.USABLE
    if kappa >= KAPPA_REFERENCE_ONLY:
        return KappaVerdict.REFERENCE_ONLY
    return KappaVerdict.UNUSABLE


def _weight(i: int, j: int, n: int, scheme: str) -> float:
    """权重矩阵：0 表示完全不一致，1 表示完全一致。"""
    if scheme == "none":
        return 1.0 if i == j else 0.0
    if n <= 1:
        return 1.0
    distance = abs(i - j) / (n - 1)
    if scheme == "quadratic":
        return 1.0 - distance**2
    return 1.0 - distance


def weighted_kappa(
    human: Sequence[int],
    judge: Sequence[int],
    *,
    weighting: Literal["linear", "quadratic", "none"] = "linear",
) -> KappaReport:
    """计算加权 Cohen's kappa。

    human / judge 是同长度的等级序列（如 0/1/2 表示差/中/好）。
    等级集合由两者的并集决定，因此调用方不必预先声明等级数。
    """
    if len(human) != len(judge):
        raise ValueError(f"长度不匹配: human={len(human)} judge={len(judge)}")
    if not human:
        raise ValueError("样本为空，无法计算 kappa")

    categories = sorted(set(human) | set(judge))
    index = {c: i for i, c in enumerate(categories)}
    n_cat = len(categories)
    total = len(human)

    # 单一等级时 kappa 无定义（无变异性）。返回 0 而非 1：
    # 全判同一档说明 Judge 没有区分能力，不该视为完美一致。
    if n_cat <= 1:
        return KappaReport(
            kappa=0.0,
            verdict=KappaVerdict.UNUSABLE,
            sample_count=total,
            weighting=weighting,
            observed_agreement=1.0,
            expected_agreement=1.0,
            underpowered=total < RECOMMENDED_SAMPLES,
        )

    human_counts = Counter(human)
    judge_counts = Counter(judge)

    observed = 0.0
    for h, j in zip(human, judge, strict=True):
        observed += _weight(index[h], index[j], n_cat, weighting)
    observed /= total

    expected = 0.0
    for h_cat, h_count in human_counts.items():
        for j_cat, j_count in judge_counts.items():
            probability = (h_count / total) * (j_count / total)
            expected += probability * _weight(
                index[h_cat], index[j_cat], n_cat, weighting
            )

    # 期望一致度为 1 时分母为 0：两者分布完全集中在同一档
    kappa = 0.0 if expected >= 1.0 else (observed - expected) / (1.0 - expected)

    return KappaReport(
        kappa=round(kappa, 4),
        verdict=_classify(kappa),
        sample_count=total,
        weighting=weighting,
        observed_agreement=round(observed, 4),
        expected_agreement=round(expected, 4),
        underpowered=total < RECOMMENDED_SAMPLES,
    )


def bucketize(scores: Sequence[float], *, edges: Sequence[float] = (60.0, 85.0)) -> list[int]:
    """把连续分数分桶为有序等级。

    kappa 要求离散等级，但 Judge 输出的是 0-100 连续分。
    默认按 docs 里的质量阈值分三档：<60 差 / 60-85 中 / ≥85 好。
    """
    result: list[int] = []
    for score in scores:
        level = 0
        for edge in edges:
            if score >= edge:
                level += 1
        result.append(level)
    return result


class KappaGateError(RuntimeError):
    """κ 不达标却被用作 blocking 断言时抛出。

    在**配置加载时**抛，而非评测时 —— 不允许"先跑起来再说"。
    """


def enforce_gate(
    report: KappaReport, *, evaluator_name: str, blocking: bool
) -> None:
    """κ 门禁。blocking 断言要求 κ ≥ 0.6 且样本量充足。"""
    if not blocking:
        return
    if report.can_block:
        return

    reason = (
        f"样本量 {report.sample_count} < {RECOMMENDED_SAMPLES}"
        if report.underpowered
        else f"κ={report.kappa:.3f} < {KAPPA_BLOCKING_MIN}"
    )
    raise KappaGateError(
        f"评估器 {evaluator_name!r} 被声明为 blocking 断言依据，但一致性不达标"
        f"（{reason}）。请改为 non-blocking、补充标注数据，或改用确定性断言。"
    )
