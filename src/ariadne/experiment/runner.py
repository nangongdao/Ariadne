"""实验编排：Dataset × Config × Evaluators。

产出逐样本结果 + 聚合指标 + 成本汇总。三条纪律：

1. **单样本失败不中断实验**。生成或评测失败的样本记为失败项并继续 ——
   跑了 500 个样本因第 499 个挂掉而全丢是不可接受的。
2. **失败样本不计入均值分母**。否则"生成侧挂了"会被读成"质量下降"。
3. **成本必须汇总**。质量提升但成本翻倍不是好交易，成本是一等指标。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from statistics import mean
from typing import Protocol

from ariadne.eval_module.base import BaseEvaluator, EvalContext, EvalResult
from ariadne.eval_module.composite import CompositeScorer
from ariadne.experiment.dataset import Dataset, DatasetItem
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

# 聚合时使用的标准指标名。与 gate.DEFAULT_RULES 对应。
METRIC_COMPOSITE = "composite_quality"
METRIC_PASS_RATE = "assertion_pass_rate"
METRIC_COST = "cost_per_item"


class Generator(Protocol):
    """被评测的系统。

    抽象成 Protocol：实验编排不关心输出来自 LLM、RAG 还是固定文件，
    这让"用离线快照跑回归"成为可能（无需真调模型，测试也快）。
    """

    def generate(self, item: DatasetItem) -> GenerationOutput: ...


@dataclass(frozen=True)
class GenerationOutput:
    text: str
    cost_usd: Decimal = Decimal("0")
    duration_ms: int = 0
    error: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.error)


@dataclass(frozen=True)
class ItemOutcome:
    """单样本结果。"""

    item_id: str
    output: str
    results: tuple[EvalResult, ...]
    composite_score: float
    passed: bool
    cost_usd: Decimal
    duration_ms: int
    # 生成侧失败（与"评测未通过"区分）
    generation_error: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def generation_failed(self) -> bool:
        return bool(self.generation_error)


@dataclass(frozen=True)
class ExperimentResult:
    """实验结果。"""

    experiment_id: str
    dataset_ref: str
    config_label: str
    outcomes: tuple[ItemOutcome, ...]

    @property
    def evaluated(self) -> tuple[ItemOutcome, ...]:
        """成功生成并评测的样本。均值只在这些上算。"""
        return tuple(o for o in self.outcomes if not o.generation_failed)

    @property
    def generation_failures(self) -> tuple[ItemOutcome, ...]:
        return tuple(o for o in self.outcomes if o.generation_failed)

    @property
    def total_cost(self) -> Decimal:
        return sum((o.cost_usd for o in self.outcomes), Decimal("0"))

    def metrics(self) -> dict[str, float]:
        """标准聚合指标。供 compare_metric 与门禁使用。"""
        evaluated = self.evaluated
        if not evaluated:
            return {METRIC_COMPOSITE: 0.0, METRIC_PASS_RATE: 0.0, METRIC_COST: 0.0}

        return {
            METRIC_COMPOSITE: round(
                mean(o.composite_score for o in evaluated), 4
            ),
            METRIC_PASS_RATE: round(
                mean(1.0 if o.passed else 0.0 for o in evaluated), 4
            ),
            METRIC_COST: round(
                float(self.total_cost) / len(self.outcomes), 8
            ),
        }

    def per_item(self, metric: str) -> dict[str, float]:
        """逐样本取值，供 diff_samples 使用。"""
        if metric == METRIC_COMPOSITE:
            return {o.item_id: o.composite_score for o in self.evaluated}
        if metric == METRIC_PASS_RATE:
            return {o.item_id: (1.0 if o.passed else 0.0) for o in self.evaluated}
        if metric == METRIC_COST:
            return {o.item_id: float(o.cost_usd) for o in self.outcomes}
        # 单个评估器的取值
        return {
            o.item_id: next(
                (r.value for r in o.results if r.name == metric), 0.0
            )
            for o in self.evaluated
        }

    def series(self, metric: str) -> list[float]:
        """按 item_id 排序的取值序列。

        排序是配对对比的前提：baseline 与 current 必须同顺序，
        否则 bootstrap 的配对差值毫无意义。
        """
        values = self.per_item(metric)
        return [values[item_id] for item_id in sorted(values)]

    def evaluator_metrics(self) -> dict[str, float]:
        """各评估器的均值。用于细粒度门禁。"""
        evaluated = self.evaluated
        if not evaluated:
            return {}
        names = {r.name for o in evaluated for r in o.results}
        return {
            name: round(
                mean(
                    r.value
                    for o in evaluated
                    for r in o.results
                    if r.name == name and not r.errored
                ),
                4,
            )
            for name in sorted(names)
            if any(
                r.name == name and not r.errored for o in evaluated for r in o.results
            )
        }

    def failure_signature_counts(self) -> dict[str, int]:
        """失败签名聚类。

        按"失败的评估器名集合"聚类，找系统性问题而非逐条看 ——
        500 个样本里 300 个同一签名说明是一个问题，不是 300 个。
        """
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            if outcome.generation_failed:
                signature = "GENERATION_FAILED"
            elif outcome.passed:
                continue
            else:
                failed = sorted(r.name for r in outcome.results if not r.passed)
                signature = "+".join(failed) if failed else "UNKNOWN"
            counts[signature] = counts.get(signature, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


class ExperimentRunner:
    """批量执行。同步实现 —— 并发留到 M5 的并行 Loop 池统一处理。"""

    def __init__(
        self,
        *,
        scorer: CompositeScorer,
        extra_evaluators: tuple[BaseEvaluator, ...] = (),
    ) -> None:
        self._scorer = scorer
        self._extra = extra_evaluators

    def run(
        self,
        *,
        experiment_id: str,
        dataset: Dataset,
        generator: Generator,
        config_label: str = "default",
        on_progress: Callable[[int, int], None] | None = None,
    ) -> ExperimentResult:
        outcomes: list[ItemOutcome] = []
        total = len(dataset)

        for index, item in enumerate(dataset, start=1):
            outcomes.append(self._run_item(item, generator))
            if on_progress:
                on_progress(index, total)

        result = ExperimentResult(
            experiment_id=experiment_id,
            dataset_ref=dataset.ref,
            config_label=config_label,
            outcomes=tuple(outcomes),
        )

        failures = len(result.generation_failures)
        if failures:
            logger.warning(
                "experiment had generation failures",
                extra={
                    "experiment_id": experiment_id,
                    "failed": failures,
                    "total": total,
                },
            )
        return result

    def _run_item(self, item: DatasetItem, generator: Generator) -> ItemOutcome:
        try:
            generated = generator.generate(item)
        except Exception as exc:
            return ItemOutcome(
                item_id=item.item_id,
                output="",
                results=(),
                composite_score=0.0,
                passed=False,
                cost_usd=Decimal("0"),
                duration_ms=0,
                generation_error=f"{type(exc).__name__}: {exc}",
                metadata=item.metadata,
            )

        if generated.failed:
            return ItemOutcome(
                item_id=item.item_id,
                output=generated.text,
                results=(),
                composite_score=0.0,
                passed=False,
                cost_usd=generated.cost_usd,
                duration_ms=generated.duration_ms,
                generation_error=generated.error,
                metadata=item.metadata,
            )

        ctx = EvalContext(
            item_id=item.item_id,
            input=item.input,
            output=generated.text,
            expected=item.expected,
            metadata=item.metadata,
        )
        composite = self._scorer.evaluate(ctx)
        extra_results = tuple(ev.evaluate(ctx) for ev in self._extra)
        all_results = composite.results + extra_results

        return ItemOutcome(
            item_id=item.item_id,
            output=generated.text,
            results=all_results,
            composite_score=composite.score,
            passed=composite.passed and all(r.passed for r in extra_results),
            cost_usd=generated.cost_usd + composite.total_cost,
            duration_ms=generated.duration_ms + composite.total_duration_ms,
            metadata=item.metadata,
        )
