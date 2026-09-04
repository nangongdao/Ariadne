"""CLI 与持久化测试。

门禁的退出码是 CI 的唯一判据，因此这里逐个验证：
0 通过 / 1 有退化 / 2 配置或数据问题。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from ariadne.eval_cli import main
from ariadne.eval_module.base import (
    BaseEvaluator,
    EvalContext,
    EvalResult,
    EvaluatorKind,
)
from ariadne.eval_module.composite import CompositeScorer, ScoreSpec
from ariadne.experiment import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_REGRESSED,
    Dataset,
    DatasetItem,
    ExperimentRunner,
    GenerationOutput,
    PersistedMeta,
    SchemaVersionError,
    load,
    save,
)
from ariadne.experiment.persist import from_dict, judge_models_of, to_dict


class ScoreByLength(BaseEvaluator):
    kind = EvaluatorKind.DETERMINISTIC

    @property
    def value_range(self) -> tuple[float, float]:
        return (0.0, 100.0)

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        score = min(len(ctx.output) * 10.0, 100.0)
        return EvalResult(
            name=self.name,
            value=score,
            passed=score >= 50.0,
            judge_model="claude-sonnet-5" if self.name == "judged" else "",
            cost_usd=Decimal("0.002"),
        )


class UniformGenerator:
    def __init__(self, chars: int, cost: str = "0.01") -> None:
        self._chars = chars
        self._cost = Decimal(cost)

    def generate(self, item: DatasetItem) -> GenerationOutput:
        return GenerationOutput(text="x" * self._chars, cost_usd=self._cost)


def dataset(n: int = 20) -> Dataset:
    return Dataset.create(
        dataset_id="d1",
        name="core",
        version=1,
        items=[DatasetItem(item_id=f"i{k:02d}", input=f"q{k}") for k in range(n)],
    )


def run_experiment(chars: int, label: str, *, cost: str = "0.01", judged: bool = False):
    name = "judged" if judged else "length"
    scorer = CompositeScorer((ScoreSpec(ScoreByLength(name)),), threshold=50.0)
    return ExperimentRunner(scorer=scorer).run(
        experiment_id=label,
        dataset=dataset(),
        generator=UniformGenerator(chars, cost),
        config_label=label,
    )


class TestPersistence:
    def test_roundtrip(self, tmp_path: Path) -> None:
        original = run_experiment(10, "base")
        path = tmp_path / "base.json"
        save(original, path)
        restored = load(path)

        assert restored.experiment_id == original.experiment_id
        assert restored.dataset_ref == original.dataset_ref
        assert len(restored.outcomes) == len(original.outcomes)
        assert restored.metrics() == original.metrics()

    def test_decimal_cost_preserved(self, tmp_path: Path) -> None:
        """成本用字符串存：float 往返会引入误差，账单对不上。"""
        original = run_experiment(10, "base", cost="0.0000001")
        path = tmp_path / "r.json"
        save(original, path)
        assert load(path).total_cost == original.total_cost

    def test_judge_models_recorded(self, tmp_path: Path) -> None:
        original = run_experiment(10, "base", judged=True)
        path = tmp_path / "r.json"
        save(original, path)
        assert judge_models_of(path) == ("claude-sonnet-5",)

    def test_explicit_meta_overrides(self, tmp_path: Path) -> None:
        path = tmp_path / "r.json"
        save(
            run_experiment(10, "base"),
            path,
            PersistedMeta(judge_models=("gpt-4o",), note="手工标注"),
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["judge_models"] == ["gpt-4o"]
        assert payload["note"] == "手工标注"

    def test_schema_version_mismatch_rejected(self) -> None:
        """格式变更时给明确提示，而非难懂的 KeyError。"""
        payload = to_dict(run_experiment(10, "base"))
        payload["schema_version"] = 999
        with pytest.raises(SchemaVersionError, match="重跑 baseline"):
            from_dict(payload)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load(tmp_path / "nope.json")

    def test_violations_survive_roundtrip(self, tmp_path: Path) -> None:
        """violations 的 span 是 M3 生成修正指令的依据，不能丢。"""
        from ariadne.eval_module.base import Severity, Violation
        from ariadne.experiment.runner import ExperimentResult, ItemOutcome

        result = ExperimentResult(
            experiment_id="e",
            dataset_ref="core@v1#abc",
            config_label="c",
            outcomes=(
                ItemOutcome(
                    item_id="i0",
                    output="o",
                    results=(
                        EvalResult(
                            name="judge",
                            value=70.0,
                            passed=False,
                            violations=(
                                Violation(
                                    dimension="factuality",
                                    span="第3段",
                                    severity=Severity.HIGH,
                                    detail="无来源",
                                ),
                            ),
                        ),
                    ),
                    composite_score=70.0,
                    passed=False,
                    cost_usd=Decimal("0.01"),
                    duration_ms=5,
                ),
            ),
        )
        path = tmp_path / "r.json"
        save(result, path)
        violation = load(path).outcomes[0].results[0].violations[0]
        assert violation.span == "第3段"
        assert violation.severity is Severity.HIGH


class TestCompareCommand:
    def _write(self, tmp_path: Path, chars: int, label: str, **kw: object) -> Path:
        path = tmp_path / f"{label}.json"
        save(run_experiment(chars, label, **kw), path)  # type: ignore[arg-type]
        return path

    def test_exit_zero_on_improvement(self, tmp_path: Path, capsys) -> None:
        base = self._write(tmp_path, 3, "base")
        curr = self._write(tmp_path, 10, "curr")
        code = main(["compare", "--baseline", str(base), "--current", str(curr)])
        assert code == EXIT_OK
        assert "通过" in capsys.readouterr().out

    def test_exit_one_on_regression(self, tmp_path: Path, capsys) -> None:
        base = self._write(tmp_path, 10, "base")
        curr = self._write(tmp_path, 3, "curr")
        code = main(["compare", "--baseline", str(base), "--current", str(curr)])
        assert code == EXIT_REGRESSED
        assert "✗" in capsys.readouterr().out

    def test_exit_two_on_missing_file(self, tmp_path: Path) -> None:
        base = self._write(tmp_path, 10, "base")
        code = main([
            "compare", "--baseline", str(base), "--current", str(tmp_path / "no.json")
        ])
        assert code == EXIT_ERROR

    def test_exit_two_on_cost_only_gate_missing_metric(self, tmp_path: Path) -> None:
        """门禁配了不存在的指标 → EXIT_ERROR，不能静默放过。"""
        base = self._write(tmp_path, 10, "base")
        curr = self._write(tmp_path, 10, "curr")
        rules = tmp_path / "gate.json"
        rules.write_text(
            json.dumps([{"metric": "not_measured", "degradation_pct": 1}]),
            encoding="utf-8",
        )
        code = main([
            "compare", "--baseline", str(base), "--current", str(curr),
            "--rules", str(rules),
        ])
        assert code == EXIT_ERROR

    def test_custom_rules_json(self, tmp_path: Path) -> None:
        base = self._write(tmp_path, 10, "base", cost="0.01")
        curr = self._write(tmp_path, 10, "curr", cost="0.011")  # +10%
        rules = tmp_path / "gate.json"
        rules.write_text(
            json.dumps({"fail_if": [{"metric": "cost_per_item", "increase_pct": 5}]}),
            encoding="utf-8",
        )
        code = main([
            "compare", "--baseline", str(base), "--current", str(curr),
            "--rules", str(rules),
        ])
        assert code == EXIT_REGRESSED

    def test_yaml_rules(self, tmp_path: Path) -> None:
        base = self._write(tmp_path, 10, "base")
        curr = self._write(tmp_path, 10, "curr")
        rules = tmp_path / "gate.yaml"
        rules.write_text(
            "fail_if:\n  - metric: composite_quality\n    degradation_pct: 3\n",
            encoding="utf-8",
        )
        code = main([
            "compare", "--baseline", str(base), "--current", str(curr),
            "--rules", str(rules),
        ])
        assert code == EXIT_OK

    def test_judge_version_mismatch_warns_not_blocks(
        self, tmp_path: Path, capsys
    ) -> None:
        """强行阻断会让"就是要换 Judge"的场景无法推进 —— 只警告。"""
        base = self._write(tmp_path, 10, "base", judged=True)
        curr = self._write(tmp_path, 10, "curr", judged=False)
        code = main(["compare", "--baseline", str(base), "--current", str(curr)])
        assert code == EXIT_OK
        assert "Judge 模型不一致" in capsys.readouterr().err

    def test_json_out_written(self, tmp_path: Path) -> None:
        base = self._write(tmp_path, 3, "base")
        curr = self._write(tmp_path, 10, "curr")
        out = tmp_path / "report.json"
        main([
            "compare", "--baseline", str(base), "--current", str(curr),
            "--json-out", str(out),
        ])
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["passed"] is True
        assert any(s["metric"] == "composite_quality" for s in payload["stats"])

    def test_dataset_mismatch_blocks(self, tmp_path: Path, capsys) -> None:
        base_result = run_experiment(10, "base")
        curr_result = run_experiment(10, "curr")
        # 手工改掉 dataset_ref 模拟用了不同数据集
        payload = to_dict(curr_result)
        payload["dataset_ref"] = "other@v1#deadbeef"
        base_path = tmp_path / "b.json"
        curr_path = tmp_path / "c.json"
        save(base_result, base_path)
        curr_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        code = main(["compare", "--baseline", str(base_path), "--current", str(curr_path)])
        assert code == EXIT_ERROR
        assert "数据集不一致" in capsys.readouterr().err

    def test_allow_mismatch_flag(self, tmp_path: Path) -> None:
        base_result = run_experiment(10, "base")
        payload = to_dict(run_experiment(10, "curr"))
        payload["dataset_ref"] = "other@v1#deadbeef"
        base_path = tmp_path / "b.json"
        curr_path = tmp_path / "c.json"
        save(base_result, base_path)
        curr_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        code = main([
            "compare", "--baseline", str(base_path), "--current", str(curr_path),
            "--allow-dataset-mismatch",
        ])
        assert code == EXIT_OK


class TestShowCommand:
    def test_prints_metrics_and_signatures(self, tmp_path: Path, capsys) -> None:
        path = tmp_path / "r.json"
        save(run_experiment(1, "low"), path)   # 10 分，全失败
        code = main(["show", "--result", str(path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "composite_quality" in out
        assert "失败签名聚类" in out
        assert "length" in out

    def test_missing_file(self, tmp_path: Path) -> None:
        assert main(["show", "--result", str(tmp_path / "no.json")]) == EXIT_ERROR
