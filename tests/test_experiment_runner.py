"""数据集与实验编排测试。

高风险点：
1. content_hash 对顺序敏感 → 导出再导入会得到不同 hash，复现校验失效
2. 生成失败被计入均值 → "生成侧挂了"会被读成"质量下降"
3. 单样本失败中断实验 → 跑 500 个因第 499 个挂掉全丢
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
    METRIC_PASS_RATE,
    Dataset,
    DatasetItem,
    DuplicateItemIdError,
    ExperimentRunner,
    GenerationOutput,
    compute_content_hash,
    parse_jsonl,
    to_jsonl,
)


def items(n: int = 3) -> list[DatasetItem]:
    return [
        DatasetItem(item_id=f"i{k}", input=f"问题{k}", expected=f"答案{k}")
        for k in range(n)
    ]


def make_dataset(n: int = 3) -> Dataset:
    return Dataset.create(
        dataset_id="d1", name="core", version=1, items=items(n)
    )


class ScoreByLength(BaseEvaluator):
    """输出越长分越高（0-100）。用于构造可预测的实验结果。"""

    kind = EvaluatorKind.DETERMINISTIC

    @property
    def value_range(self) -> tuple[float, float]:
        return (0.0, 100.0)

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        score = min(len(ctx.output) * 10.0, 100.0)
        return EvalResult(name=self.name, value=score, passed=score >= 50.0)


class FixedGenerator:
    """按 item_id 返回预设输出。"""

    def __init__(self, outputs: dict[str, GenerationOutput]) -> None:
        self._outputs = outputs

    def generate(self, item: DatasetItem) -> GenerationOutput:
        return self._outputs.get(
            item.item_id, GenerationOutput(text="默认", cost_usd=Decimal("0.001"))
        )


class ExplodingGenerator:
    def generate(self, item: DatasetItem) -> GenerationOutput:
        if item.item_id == "i1":
            raise RuntimeError("provider 超时")
        return GenerationOutput(text="正常输出", cost_usd=Decimal("0.001"))


def scorer() -> CompositeScorer:
    return CompositeScorer(
        (ScoreSpec(ScoreByLength("length")),), threshold=50.0
    )


class TestContentHash:
    def test_order_insensitive(self) -> None:
        """同一组样本换顺序仍是同一数据集 —— 否则导出再导入 hash 会变。"""
        forward = items(5)
        reversed_items = list(reversed(forward))
        assert compute_content_hash(forward) == compute_content_hash(reversed_items)

    def test_content_change_alters_hash(self) -> None:
        base = items(3)
        modified = [*base[:-1], DatasetItem(item_id="i2", input="不同的问题")]
        assert compute_content_hash(base) != compute_content_hash(modified)

    def test_metadata_order_insensitive(self) -> None:
        a = [DatasetItem(item_id="i", input="q", metadata={"x": "1", "y": "2"})]
        b = [DatasetItem(item_id="i", input="q", metadata={"y": "2", "x": "1"})]
        assert compute_content_hash(a) == compute_content_hash(b)

    def test_verify_detects_tampering(self) -> None:
        dataset = make_dataset()
        assert dataset.verify()
        tampered = Dataset(
            dataset_id=dataset.dataset_id,
            name=dataset.name,
            version=dataset.version,
            items=(*dataset.items, DatasetItem(item_id="extra", input="x")),
            content_hash=dataset.content_hash,
        )
        assert not tampered.verify()


class TestDataset:
    def test_duplicate_ids_rejected(self) -> None:
        """静默去重会让样本级 diff 对不上。"""
        dupes = [
            DatasetItem(item_id="same", input="a"),
            DatasetItem(item_id="same", input="b"),
        ]
        with pytest.raises(DuplicateItemIdError, match="重复 item_id"):
            Dataset.create(dataset_id="d", name="n", version=1, items=dupes)

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="不能为空"):
            Dataset.create(dataset_id="d", name="n", version=1, items=[])

    def test_ref_format(self) -> None:
        dataset = make_dataset()
        assert dataset.ref.startswith("core@v1#")
        assert len(dataset.ref.split("#")[1]) == 16

    def test_slice_by_metadata(self) -> None:
        tagged = [
            DatasetItem(item_id="e1", input="q", metadata={"difficulty": "easy"}),
            DatasetItem(item_id="h1", input="q", metadata={"difficulty": "hard"}),
            DatasetItem(item_id="h2", input="q", metadata={"difficulty": "hard"}),
        ]
        dataset = Dataset.create(dataset_id="d", name="n", version=1, items=tagged)
        assert len(dataset.slice_by("difficulty", "hard")) == 2
        assert dataset.slice_keys("difficulty") == ("easy", "hard")


class TestJsonl:
    def test_roundtrip_preserves_hash(self) -> None:
        """往返转换后 content_hash 必须不变，否则复现校验形同虚设。"""
        original = make_dataset(5)
        exported = to_jsonl(original)
        reimported = Dataset.create(
            dataset_id=original.dataset_id,
            name=original.name,
            version=original.version,
            items=parse_jsonl(exported.splitlines()),
        )
        assert reimported.content_hash == original.content_hash

    def test_bad_line_raises_not_skipped(self) -> None:
        """数据集不容错：静默跳过坏行会导致两次实验用的不是同一数据集。"""
        with pytest.raises(ValueError, match="第 2 行"):
            parse_jsonl(['{"input":"ok"}', "{broken"])

    def test_missing_input_raises(self) -> None:
        with pytest.raises(ValueError, match="缺少 input"):
            parse_jsonl(['{"item_id":"a"}'])

    def test_comments_and_blanks_skipped(self) -> None:
        parsed = parse_jsonl(["// 注释", "", '{"input":"q"}'])
        assert len(parsed) == 1

    def test_all_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="没有有效样本"):
            parse_jsonl(["// 只有注释"])


class TestRunner:
    def test_basic_run(self) -> None:
        dataset = make_dataset(3)
        generator = FixedGenerator({
            "i0": GenerationOutput(text="x" * 10, cost_usd=Decimal("0.01")),
            "i1": GenerationOutput(text="x" * 10, cost_usd=Decimal("0.01")),
            "i2": GenerationOutput(text="x" * 10, cost_usd=Decimal("0.01")),
        })
        result = ExperimentRunner(scorer=scorer()).run(
            experiment_id="e1", dataset=dataset, generator=generator
        )
        assert len(result.outcomes) == 3
        assert result.metrics()[METRIC_COMPOSITE] == 100.0
        assert result.metrics()[METRIC_PASS_RATE] == 1.0

    def test_generation_exception_does_not_stop_run(self) -> None:
        """跑 500 个样本因一个挂掉而全丢是不可接受的。"""
        result = ExperimentRunner(scorer=scorer()).run(
            experiment_id="e1", dataset=make_dataset(3), generator=ExplodingGenerator()
        )
        assert len(result.outcomes) == 3
        assert len(result.generation_failures) == 1
        assert "RuntimeError" in result.generation_failures[0].generation_error

    def test_generation_failures_excluded_from_mean(self) -> None:
        """否则"生成侧挂了"会被读成"质量下降"。"""
        generator = FixedGenerator({
            "i0": GenerationOutput(text="x" * 10),
            "i1": GenerationOutput(text="", error="provider 429"),
            "i2": GenerationOutput(text="x" * 10),
        })
        result = ExperimentRunner(scorer=scorer()).run(
            experiment_id="e1", dataset=make_dataset(3), generator=generator
        )
        # 只有两个成功样本参与均值，都是满分
        assert result.metrics()[METRIC_COMPOSITE] == 100.0
        assert len(result.evaluated) == 2

    def test_cost_per_item_uses_all_items(self) -> None:
        """成本分母是全部样本 —— 失败的调用也花了钱。"""
        generator = FixedGenerator({
            "i0": GenerationOutput(text="ok", cost_usd=Decimal("0.02")),
            "i1": GenerationOutput(text="", error="失败", cost_usd=Decimal("0.02")),
        })
        result = ExperimentRunner(scorer=scorer()).run(
            experiment_id="e1", dataset=make_dataset(2), generator=generator
        )
        assert result.total_cost == Decimal("0.04")
        assert result.metrics()[METRIC_COST] == 0.02

    def test_progress_callback(self) -> None:
        seen: list[tuple[int, int]] = []
        ExperimentRunner(scorer=scorer()).run(
            experiment_id="e1",
            dataset=make_dataset(3),
            generator=FixedGenerator({}),
            on_progress=lambda done, total: seen.append((done, total)),
        )
        assert seen == [(1, 3), (2, 3), (3, 3)]

    def test_series_sorted_for_pairing(self) -> None:
        """baseline 与 current 必须同顺序，否则配对 bootstrap 无意义。"""
        result = ExperimentRunner(scorer=scorer()).run(
            experiment_id="e1",
            dataset=make_dataset(3),
            generator=FixedGenerator({
                "i0": GenerationOutput(text="x"),
                "i1": GenerationOutput(text="x" * 5),
                "i2": GenerationOutput(text="x" * 10),
            }),
        )
        assert result.series(METRIC_COMPOSITE) == [10.0, 50.0, 100.0]

    def test_failure_signature_clustering(self) -> None:
        """500 个样本里 300 个同一签名说明是一个问题，不是 300 个。"""
        generator = FixedGenerator({
            "i0": GenerationOutput(text="x"),      # 低分，length 失败
            "i1": GenerationOutput(text="x"),      # 同签名
            "i2": GenerationOutput(text="x" * 10),  # 通过
        })
        result = ExperimentRunner(scorer=scorer()).run(
            experiment_id="e1", dataset=make_dataset(3), generator=generator
        )
        signatures = result.failure_signature_counts()
        assert signatures["length"] == 2

    def test_generation_failure_has_own_signature(self) -> None:
        result = ExperimentRunner(scorer=scorer()).run(
            experiment_id="e1", dataset=make_dataset(3), generator=ExplodingGenerator()
        )
        assert result.failure_signature_counts()["GENERATION_FAILED"] == 1

    def test_evaluator_metrics_breakdown(self) -> None:
        result = ExperimentRunner(scorer=scorer()).run(
            experiment_id="e1",
            dataset=make_dataset(2),
            generator=FixedGenerator({
                "i0": GenerationOutput(text="x" * 10),
                "i1": GenerationOutput(text="x" * 10),
            }),
        )
        assert result.evaluator_metrics()["length"] == 100.0

    def test_dataset_ref_recorded(self) -> None:
        """实验必须记录数据集版本与 hash，否则无法复现。"""
        dataset = make_dataset()
        result = ExperimentRunner(scorer=scorer()).run(
            experiment_id="e1", dataset=dataset, generator=FixedGenerator({})
        )
        assert result.dataset_ref == dataset.ref
