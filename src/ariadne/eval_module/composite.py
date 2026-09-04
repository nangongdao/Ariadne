"""复合评分与评测套件编排。

复合分是"质量得分 ≥ 85"这类断言的实现。两个容易做错的地方：

1. **量纲**。各评估器的 value 量纲不同（相似度 0-1、字数 0-∞、通过 0/1），
   直接加权求和毫无意义。必须先按各自的 value_range 归一化。
2. **errored 的处理**。评估器自身出错时不能当 0 分算入 —— 那会把
   "我们没测出来"混同为"输出很差"，让 Loop 朝错误方向修正。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

from ariadne.eval_module.base import (
    BaseEvaluator,
    EvalContext,
    EvalResult,
    ThresholdOp,
    Violation,
    compare,
)

# 复合分统一到 0-100：与 docs 里"质量得分 ≥ 85"的表述一致
COMPOSITE_SCALE = 100.0


@dataclass(frozen=True)
class ScoreSpec:
    """单个评估器在复合分中的配置。"""

    evaluator: BaseEvaluator
    weight: float = 1.0
    # 该项自身是否算通过（独立于复合分阈值）。给了就**覆盖**评估器自带的
    # passed 判定 —— 见 CompositeScorer._apply_spec_threshold 的说明。
    threshold: float | None = None
    op: ThresholdOp = ThresholdOp.GTE
    # 归一化上界。value_range 为无穷时必须显式给出，否则无法归一化
    normalize_max: float | None = None


@dataclass(frozen=True)
class CompositeResult:
    """复合评测结果。"""

    score: float
    passed: bool
    results: tuple[EvalResult, ...]
    # 归一化后的各项贡献，用于前端画雷达图/堆叠柱
    contributions: dict[str, float] = field(default_factory=dict)
    errored_names: tuple[str, ...] = ()

    @property
    def total_cost(self) -> Decimal:
        return sum((r.cost_usd for r in self.results), Decimal("0"))

    @property
    def total_duration_ms(self) -> int:
        return sum(r.duration_ms for r in self.results)

    @property
    def failed_names(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.results if not r.passed)

    @property
    def violations(self) -> tuple[Violation, ...]:
        """汇总所有 Judge 定位到的问题，供 M3 的 Critique 使用。"""
        return tuple(v for r in self.results for v in r.violations)


def normalize(value: float, low: float, high: float) -> float:
    """把 value 线性映射到 0-1 并裁剪。

    上界为无穷时无法归一化，调用方必须提供 normalize_max ——
    静默当成 1.0 会让权重失效，是难查的 bug。
    """
    if not math.isfinite(value):
        return 0.0
    if not math.isfinite(low) or not math.isfinite(high):
        raise ValueError(
            f"无法归一化：区间 [{low}, {high}] 含无穷值，请提供 normalize_max"
        )
    if high <= low:
        return 1.0 if value >= high else 0.0
    return min(max((value - low) / (high - low), 0.0), 1.0)


def _apply_spec_threshold(spec: ScoreSpec, result: EvalResult) -> EvalResult:
    """用 spec 的 threshold/op 覆盖评估器自带的 passed 判定。

    `ScoreSpec.threshold` 与 `op` 两个字段曾经**只被写、从不被读**：
    `eval_worker.build_scorer_from_config` 逐条填 `threshold=sc.get("threshold")`，
    而 `CompositeScorer.evaluate` 只用 `result.passed`。于是配置里的
    `{"type": "rouge_l", "threshold": 0.9}` 实际按评估器构造器的默认线判定
    （rouge_l 默认 0.5），配 0.9 和不配没有区别 —— 与 R12 的"实现了但没接线"
    是同一类，只不过死掉的是两个字段而非一个模块。

    覆盖而非取交集：两处阈值语义相同（该项达标线），取交集会让
    `threshold` 只能收紧不能放宽，而放宽正是这个字段存在的理由 ——
    评估器的默认线是通用值，实验想要自己的线。

    errored 的结果直接返回：出错时 value 无意义（通常是 0.0），套阈值
    只会把"没测出来"变成"没达标"，正是本模块开头警告的那类混同。
    """
    if spec.threshold is None or result.errored:
        return result
    passed = compare(result.value, spec.op, spec.threshold)
    if passed == result.passed:
        return result
    evidence = (
        f"{result.evidence}\n" if result.evidence else ""
    ) + f"[spec 阈值] value={result.value} {spec.op.value} {spec.threshold} → {passed}"
    return result.model_copy(update={"passed": passed, "evidence": evidence})


class CompositeScorer:
    """加权复合评分。

    权重不要求和为 1 —— 内部按总权重归一化，这样增删项时不必重算所有权重。
    """

    def __init__(self, specs: tuple[ScoreSpec, ...], *, threshold: float = 85.0) -> None:
        if not specs:
            raise ValueError("CompositeScorer 至少需要一个 ScoreSpec")
        if any(s.weight < 0 for s in specs):
            raise ValueError("权重不能为负")
        if sum(s.weight for s in specs) <= 0:
            raise ValueError("权重之和必须为正")

        self._specs = specs
        self._threshold = threshold

    def evaluate(self, ctx: EvalContext) -> CompositeResult:
        results: list[EvalResult] = []
        contributions: dict[str, float] = {}
        errored: list[str] = []

        weighted_sum = 0.0
        effective_weight = 0.0

        for spec in self._specs:
            result = _apply_spec_threshold(spec, spec.evaluator.evaluate(ctx))
            results.append(result)

            if result.errored:
                # 不计入分母：出错的项既不加分也不扣分，
                # 否则"没测出来"会被当成"很差"
                errored.append(result.name)
                continue

            low, high = spec.evaluator.value_range
            if spec.normalize_max is not None:
                high = spec.normalize_max
            try:
                unit = normalize(result.value, low, high)
            except ValueError:
                # 配置缺 normalize_max：按 passed 退化为二值，并记为异常项
                unit = 1.0 if result.passed else 0.0
                errored.append(result.name)

            contributions[result.name] = round(unit * spec.weight, 6)
            weighted_sum += unit * spec.weight
            effective_weight += spec.weight

        score = (
            0.0
            if effective_weight == 0
            else round(weighted_sum / effective_weight * COMPOSITE_SCALE, 2)
        )

        # 全部项都出错时不能判"通过"——那是掩盖故障
        all_errored = effective_weight == 0
        passed = (
            not all_errored
            and compare(score, ThresholdOp.GTE, self._threshold)
            and all(r.passed for r in results if not r.errored)
        )

        return CompositeResult(
            score=score,
            passed=passed,
            results=tuple(results),
            contributions=contributions,
            errored_names=tuple(errored),
        )

    @property
    def threshold(self) -> float:
        return self._threshold


class EvalSuite:
    """一组评估器的批量执行（不加权，各自独立判定）。

    与 CompositeScorer 的区别：suite 只是并列跑一堆评估器并汇总
    pass/fail，不算复合分。M3 的断言集用它，因为断言是"全部必须通过"
    而非"加权平均达标"。
    """

    def __init__(self, evaluators: tuple[BaseEvaluator, ...]) -> None:
        self._evaluators = evaluators

    def run(self, ctx: EvalContext) -> tuple[EvalResult, ...]:
        return tuple(ev.evaluate(ctx) for ev in self._evaluators)

    def all_passed(self, ctx: EvalContext) -> bool:
        return all(r.passed for r in self.run(ctx))
