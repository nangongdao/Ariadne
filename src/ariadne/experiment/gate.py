"""CI 回归门禁。

设计要点：**成本也是门禁维度**。质量提升 1% 但成本翻倍通常不是好交易，
只看质量的门禁会放过这种退化。

退出码语义（供 CI 使用）：
  0  通过
  1  有指标退化超阈值
  2  配置或数据问题（无法判定）
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from ariadne.experiment.stats import ComparisonStat, Direction

EXIT_OK = 0
EXIT_REGRESSED = 1
EXIT_ERROR = 2


class GateKind(StrEnum):
    """门禁方向。"""

    DEGRADATION = "degradation"  # 指标下降超阈值则阻断（质量类）
    INCREASE = "increase"        # 指标上升超阈值则阻断（成本/延迟类）


@dataclass(frozen=True)
class GateRule:
    """单条门禁规则。"""

    metric: str
    kind: GateKind
    # 允许的变化上限（百分比）。0 表示不允许任何退化
    max_pct: float
    # 仅在差异显著时阻断。对噪声大的指标设 True 可减少误报，
    # 但对"绝不允许退化"的指标应设 False
    require_significant: bool = True
    note: str = ""


@dataclass(frozen=True)
class GateViolation:
    rule: GateRule
    stat: ComparisonStat
    actual_pct: float

    def describe(self) -> str:
        verb = "下降" if self.rule.kind is GateKind.DEGRADATION else "上涨"
        return (
            f"{self.rule.metric} {verb} {abs(self.actual_pct):.2f}%"
            f"，超过允许的 {self.rule.max_pct:.2f}%"
            f"（{self.stat.baseline_mean:.4f} → {self.stat.current_mean:.4f}"
            f"，CI[{self.stat.ci_low:+.4f}, {self.stat.ci_high:+.4f}]）"
            + (f" — {self.rule.note}" if self.rule.note else "")
        )


@dataclass(frozen=True)
class GateReport:
    violations: tuple[GateViolation, ...]
    checked: tuple[str, ...]
    missing: tuple[str, ...] = ()
    inconclusive: tuple[str, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return not self.violations and not self.missing

    @property
    def exit_code(self) -> int:
        """缺指标算 EXIT_ERROR 而非放过 —— 门禁配了却没测到，
        通常意味着评估器名写错或实验没跑完，静默放过会让门禁形同虚设。"""
        if self.missing:
            return EXIT_ERROR
        return EXIT_REGRESSED if self.violations else EXIT_OK

    def describe(self) -> str:
        lines: list[str] = []
        if self.missing:
            lines.append(f"✗ 门禁配置的指标未在实验结果中找到: {', '.join(self.missing)}")
        for violation in self.violations:
            lines.append(f"✗ {violation.describe()}")
        if self.inconclusive:
            lines.append(
                f"⚠ 以下指标样本量不足或方差过大，未参与判定: "
                f"{', '.join(self.inconclusive)}"
            )
        if self.passed:
            lines.append(f"✓ 全部 {len(self.checked)} 项门禁通过")
        return "\n".join(lines)


def evaluate_gate(
    rules: Sequence[GateRule], stats: Sequence[ComparisonStat]
) -> GateReport:
    """执行门禁判定。"""
    by_metric = {s.metric: s for s in stats}
    violations: list[GateViolation] = []
    checked: list[str] = []
    missing: list[str] = []
    inconclusive: list[str] = []

    for rule in rules:
        stat = by_metric.get(rule.metric)
        if stat is None:
            missing.append(rule.metric)
            continue

        checked.append(rule.metric)

        if stat.direction is Direction.INCONCLUSIVE:
            inconclusive.append(rule.metric)
            continue

        if rule.require_significant and not stat.significant:
            continue

        # DEGRADATION 关注下降（delta_pct 为负），INCREASE 关注上涨
        actual = (
            -stat.delta_pct
            if rule.kind is GateKind.DEGRADATION
            else stat.delta_pct
        )

        if actual > rule.max_pct:
            violations.append(
                GateViolation(rule=rule, stat=stat, actual_pct=actual)
            )

    return GateReport(
        violations=tuple(violations),
        checked=tuple(checked),
        missing=tuple(missing),
        inconclusive=tuple(inconclusive),
    )


# 默认门禁：docs/05 里的示例配置。
# assertion_pass_rate 不允许任何下降，且不要求显著性 ——
# 断言通过率的退化是硬性问题，不该被"样本少所以不显著"放过。
DEFAULT_RULES: tuple[GateRule, ...] = (
    GateRule(
        metric="composite_quality",
        kind=GateKind.DEGRADATION,
        max_pct=3.0,
        note="质量均值下降超 3% 阻断合并",
    ),
    GateRule(
        metric="assertion_pass_rate",
        kind=GateKind.DEGRADATION,
        max_pct=0.0,
        require_significant=False,
        note="断言通过率不允许任何下降",
    ),
    GateRule(
        metric="cost_per_item",
        kind=GateKind.INCREASE,
        max_pct=20.0,
        note="成本上涨超 20% 需人工确认",
    ),
)


def parse_rules(raw: Sequence[dict[str, object]]) -> tuple[GateRule, ...]:
    """从配置字典构造规则。字段名与 docs/05 的 YAML 示例一致。"""
    rules: list[GateRule] = []
    for item in raw:
        metric = str(item.get("metric", "")).strip()
        if not metric:
            raise ValueError(f"门禁规则缺少 metric: {item!r}")

        if "degradation_pct" in item:
            kind = GateKind.DEGRADATION
            max_pct = float(item["degradation_pct"])  # type: ignore[arg-type]
        elif "increase_pct" in item:
            kind = GateKind.INCREASE
            max_pct = float(item["increase_pct"])  # type: ignore[arg-type]
        else:
            raise ValueError(
                f"门禁规则 {metric!r} 必须指定 degradation_pct 或 increase_pct"
            )

        rules.append(
            GateRule(
                metric=metric,
                kind=kind,
                max_pct=max_pct,
                require_significant=bool(item.get("require_significant", True)),
                note=str(item.get("note", "")),
            )
        )
    return tuple(rules)
