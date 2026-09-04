"""元评测测试。

元评测的价值在于**用已知答案的对抗集抓住 Judge 的退化**。因此这里的测试
要验证两件事：
1. 对抗集本身能区分好坏 Judge（用桩 Judge 模拟）
2. "评估器出错"不能被算成"判对了"
"""

from __future__ import annotations

import pytest

from ariadne.eval_module.base import (
    BaseEvaluator,
    EvalContext,
    EvalResult,
    EvaluatorKind,
)
from ariadne.eval_module.judge.meta import (
    BUILTIN_CASES,
    META_ACCURACY_MIN,
    AdversarialCase,
    Expectation,
    MetaEvaluation,
    builtin_meta_evaluation,
)


class PerfectJudge(BaseEvaluator):
    """完美 Judge：按样本预期作答（作弊，仅用于验证元评测框架本身）。"""

    kind = EvaluatorKind.JUDGE

    def __init__(self, cases: tuple[AdversarialCase, ...]) -> None:
        super().__init__("perfect")
        self._answers = {
            c.case_id: c.expectation is Expectation.SHOULD_PASS for c in cases
        }

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        should_pass = self._answers.get(ctx.item_id, True)
        return EvalResult(
            name=self.name,
            value=95.0 if should_pass else 40.0,
            passed=should_pass,
        )


class AlwaysPassJudge(BaseEvaluator):
    """总是判通过 —— 模拟"被文采迷惑、什么都放过"的坏 Judge。"""

    kind = EvaluatorKind.JUDGE

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        return EvalResult(name=self.name, value=95.0, passed=True)


class BrokenJudge(BaseEvaluator):
    """总是出错。"""

    kind = EvaluatorKind.JUDGE

    def _evaluate(self, ctx: EvalContext) -> EvalResult:
        raise RuntimeError("provider 挂了")


class TestFramework:
    def test_perfect_judge_scores_full(self) -> None:
        meta = builtin_meta_evaluation()
        report = meta.run(PerfectJudge(BUILTIN_CASES))
        assert report.accuracy == 1.0
        assert report.healthy
        assert not report.failed_cases

    def test_always_pass_judge_caught(self) -> None:
        """对抗集必须能抓住"什么都放过"的 Judge。"""
        report = builtin_meta_evaluation().run(AlwaysPassJudge("lenient"))
        assert report.accuracy < META_ACCURACY_MIN
        assert not report.healthy
        # 该判失败的样本全被放过了
        assert len(report.failed_cases) >= 5

    def test_errored_counts_as_incorrect(self) -> None:
        """"没测出来"不能算"判对了"。"""
        report = builtin_meta_evaluation().run(BrokenJudge("broken"))
        assert report.accuracy == 0.0
        assert report.errored == report.total
        assert not report.healthy

    def test_empty_case_set_rejected(self) -> None:
        with pytest.raises(ValueError, match="对抗集为空"):
            MetaEvaluation(())

    def test_by_trap_breakdown(self) -> None:
        """按陷阱类型统计，便于定位 Judge 在哪类问题上失灵。"""
        report = builtin_meta_evaluation().run(AlwaysPassJudge("lenient"))
        assert "eloquent_emptiness" in report.by_trap
        # 该判失败的类别准确率为 0
        assert report.by_trap["eloquent_emptiness"] == 0.0
        # 正例类别仍是 1.0
        assert report.by_trap["compliant_baseline"] == 1.0

    def test_describe_readable(self) -> None:
        report = builtin_meta_evaluation().run(AlwaysPassJudge("lenient"))
        text = report.describe()
        assert "准确率" in text
        assert "低于下限" in text


class TestBuiltinCaseSet:
    def test_covers_all_three_categories(self) -> None:
        """docs/05 要求覆盖三类：事实错误、指令遵循、文采空洞。"""
        traps = {c.trap for c in BUILTIN_CASES}
        assert "fabricated_fact" in traps
        assert "ignored_constraint" in traps
        assert "eloquent_emptiness" in traps

    def test_has_both_positive_and_negative(self) -> None:
        """只有负例的对抗集会奖励"什么都拒绝"的 Judge。"""
        expectations = {c.expectation for c in BUILTIN_CASES}
        assert expectations == {Expectation.SHOULD_PASS, Expectation.SHOULD_FAIL}

    def test_every_case_has_note(self) -> None:
        """每条都要说明考察意图，否则后来者无法判断新增样本是否重复。"""
        for case in BUILTIN_CASES:
            assert case.note, f"{case.case_id} 缺少 note"
            assert case.trap, f"{case.case_id} 缺少 trap 分类"

    def test_case_ids_unique(self) -> None:
        ids = [c.case_id for c in BUILTIN_CASES]
        assert len(ids) == len(set(ids))

    def test_eloquent_emptiness_pair_exists(self) -> None:
        """"华丽空洞"必须配一条"简洁可操作"的正例，
        否则无法区分"识别空洞"与"单纯讨厌长文本"。"""
        by_trap = {c.trap: c for c in BUILTIN_CASES}
        assert by_trap["eloquent_emptiness"].expectation is Expectation.SHOULD_FAIL
        assert by_trap["concise_baseline"].expectation is Expectation.SHOULD_PASS
        # 两条应针对同一 task，才构成有效对照
        assert by_trap["eloquent_emptiness"].task == by_trap["concise_baseline"].task
