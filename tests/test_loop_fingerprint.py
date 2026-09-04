"""振荡与停滞检测测试。

Loop 的第二大失效模式是原地打转。这里的每条测试对应一种真实的
打转形态 —— 漏掉任一种都会让 Loop 默默烧完预算。
"""

from __future__ import annotations

import pytest

from ariadne.loop_module.fingerprint import (
    IterationTrace,
    OscillationDetector,
    OscillationVerdict,
    failure_fingerprint,
    normalize_output,
    output_fingerprint,
)


def trace(
    iteration: int,
    *,
    output: str = "",
    failed: tuple[str, ...] = (),
    score: float = 50.0,
) -> IterationTrace:
    return IterationTrace(
        iteration=iteration,
        output_fp=output_fingerprint(output or f"output-{iteration}"),
        failure_fp=failure_fingerprint(failed),
        score=score,
        failed_ids=failed,
    )


class TestNormalization:
    def test_collapses_whitespace(self) -> None:
        assert normalize_output("a    b") == normalize_output("a b")

    def test_strips_trailing_space(self) -> None:
        assert normalize_output("line  \n") == normalize_output("line")

    def test_collapses_excess_blank_lines(self) -> None:
        assert normalize_output("a\n\n\n\nb") == normalize_output("a\n\nb")

    def test_preserves_punctuation_differences(self) -> None:
        """不去标点：去掉会把"只改了标点"的真实改动误判为无变化。"""
        assert normalize_output("hello.") != normalize_output("hello!")

    def test_preserves_case(self) -> None:
        assert normalize_output("Hello") != normalize_output("hello")


class TestFingerprints:
    def test_output_fp_stable(self) -> None:
        assert output_fingerprint("x") == output_fingerprint("x")

    def test_output_fp_ignores_whitespace_noise(self) -> None:
        assert output_fingerprint("a  b") == output_fingerprint("a b")

    def test_failure_fp_order_insensitive(self) -> None:
        """断言求值顺序不该影响签名 —— 否则振荡检测完全失效。"""
        assert failure_fingerprint(["a", "b"]) == failure_fingerprint(["b", "a"])

    def test_failure_fp_empty_is_empty_string(self) -> None:
        """全通过时无失败签名，不该产生一个"空集合"的哈希。"""
        assert failure_fingerprint([]) == ""

    def test_different_failures_differ(self) -> None:
        assert failure_fingerprint(["a"]) != failure_fingerprint(["a", "b"])


class TestRepeatedFailureSignature:
    def test_first_occurrence_is_clean(self) -> None:
        detector = OscillationDetector()
        report = detector.record(trace(1, failed=("fmt",)))
        assert report.verdict is OscillationVerdict.NONE

    def test_two_consecutive_escalates(self) -> None:
        """连续 2 轮同签名 → 换解法（提温度/换模型/禁用上轮路径）。"""
        detector = OscillationDetector()
        detector.record(trace(1, failed=("fmt",), score=50))
        report = detector.record(trace(2, failed=("fmt",), score=60))
        assert report.verdict is OscillationVerdict.ESCALATE
        assert report.repeat_count == 2
        assert report.verdict.should_escalate
        assert not report.verdict.should_terminate

    def test_three_consecutive_stalls(self) -> None:
        """连续 3 轮同签名 → 反馈信号无效，终止。"""
        detector = OscillationDetector()
        for i, score in enumerate([40.0, 60.0, 80.0], start=1):
            report = detector.record(trace(i, failed=("fmt",), score=score))
        assert report.verdict is OscillationVerdict.STALLED
        assert report.verdict.should_terminate
        assert "反馈信号无效" in report.reason

    def test_changing_signature_resets_count(self) -> None:
        """签名变了说明反馈在起作用，不该累计。"""
        detector = OscillationDetector()
        detector.record(trace(1, failed=("fmt",), score=40))
        detector.record(trace(2, failed=("fmt",), score=60))
        report = detector.record(trace(3, failed=("cite",), score=80))
        assert report.verdict is OscillationVerdict.NONE

    def test_all_passing_does_not_count(self) -> None:
        """无失败断言时不该触发任何振荡判定。"""
        detector = OscillationDetector()
        for i in range(1, 5):
            report = detector.record(
                trace(i, failed=(), score=float(i * 20))
            )
            assert report.verdict is OscillationVerdict.NONE


class TestOutputRepetition:
    def test_identical_output_is_oscillating(self) -> None:
        """输出与历史某轮完全相同 —— 最明确的振荡信号。"""
        detector = OscillationDetector()
        detector.record(trace(1, output="same text", failed=("a",), score=50))
        detector.record(trace(2, output="different", failed=("b",), score=60))
        report = detector.record(trace(3, output="same text", failed=("c",), score=70))

        assert report.verdict is OscillationVerdict.OSCILLATING
        assert report.duplicate_of == 1
        assert report.verdict.should_escalate

    def test_output_repetition_takes_priority(self) -> None:
        """输出重复比签名重复更明确，应优先报告。"""
        detector = OscillationDetector()
        detector.record(trace(1, output="X", failed=("a",), score=50))
        report = detector.record(trace(2, output="X", failed=("a",), score=50))
        assert report.verdict is OscillationVerdict.OSCILLATING

    def test_whitespace_only_change_counts_as_repeat(self) -> None:
        """只改空白等于没改。"""
        detector = OscillationDetector()
        detector.record(trace(1, output="a b", failed=("x",)))
        report = detector.record(trace(2, output="a    b", failed=("x",)))
        assert report.verdict is OscillationVerdict.OSCILLATING


class TestDiminishingReturns:
    def test_flat_scores_stall(self) -> None:
        """得分不涨就提前终止省成本。"""
        detector = OscillationDetector(stall_threshold=2.0, stall_patience=2)
        detector.record(trace(1, failed=("a",), score=70.0))
        detector.record(trace(2, failed=("b",), score=70.5))
        report = detector.record(trace(3, failed=("c",), score=71.0))
        assert report.verdict is OscillationVerdict.STALLED
        assert "收益递减" in report.reason

    def test_rising_scores_continue(self) -> None:
        detector = OscillationDetector(stall_threshold=2.0, stall_patience=2)
        detector.record(trace(1, failed=("a",), score=50.0))
        detector.record(trace(2, failed=("b",), score=65.0))
        report = detector.record(trace(3, failed=("c",), score=80.0))
        assert report.verdict is OscillationVerdict.NONE

    def test_needs_enough_history(self) -> None:
        """patience=2 需要 3 轮数据才能算出 2 个增量。"""
        detector = OscillationDetector(stall_patience=2)
        detector.record(trace(1, failed=("a",), score=70.0))
        report = detector.record(trace(2, failed=("b",), score=70.1))
        assert report.verdict is OscillationVerdict.NONE

    def test_declining_scores_also_stall(self) -> None:
        """得分下降同样是收益递减（增量为负 < 阈值）。"""
        detector = OscillationDetector(stall_threshold=2.0, stall_patience=2)
        detector.record(trace(1, failed=("a",), score=80.0))
        detector.record(trace(2, failed=("b",), score=70.0))
        report = detector.record(trace(3, failed=("c",), score=60.0))
        assert report.verdict is OscillationVerdict.STALLED

    def test_patience_configurable(self) -> None:
        detector = OscillationDetector(stall_threshold=5.0, stall_patience=3)
        for i, score in enumerate([50.0, 51.0, 52.0], start=1):
            report = detector.record(trace(i, failed=(f"a{i}",), score=score))
        # patience=3 需要 4 轮
        assert report.verdict is OscillationVerdict.NONE
        report = detector.record(trace(4, failed=("a4",), score=53.0))
        assert report.verdict is OscillationVerdict.STALLED


class TestForbidHints:
    def test_hints_list_historical_failures(self) -> None:
        """把"这些路走过了"显式告诉模型是防振荡的关键手段。"""
        detector = OscillationDetector()
        detector.record(trace(1, failed=("fmt",), score=50))
        report = detector.record(trace(2, failed=("fmt",), score=55))
        assert report.forbid_hints
        assert any("轮次 1" in h for h in report.forbid_hints)
        assert any("fmt" in h for h in report.forbid_hints)

    def test_hints_deduplicated(self) -> None:
        """同一失败组合只列一次，避免 forbidden 段膨胀。"""
        detector = OscillationDetector()
        for i in range(1, 4):
            report = detector.record(trace(i, failed=("fmt",), score=50.0 + i * 5))
        assert len(report.forbid_hints) == 1

    def test_no_hints_when_clean(self) -> None:
        detector = OscillationDetector()
        report = detector.record(trace(1, failed=(), score=90))
        assert report.forbid_hints == ()


class TestCheckpointRestore:
    def test_restore_preserves_detection(self) -> None:
        """不恢复历史会让振荡检测在崩溃后失效 —— 新 Worker 看不到历史，
        以为每轮都是第一次见到该失败签名。"""
        original = OscillationDetector()
        original.record(trace(1, failed=("fmt",), score=50))
        original.record(trace(2, failed=("fmt",), score=55))

        resumed = OscillationDetector()
        resumed.restore(original.history)
        report = resumed.record(trace(3, failed=("fmt",), score=60))
        assert report.verdict is OscillationVerdict.STALLED

    def test_without_restore_detection_is_lost(self) -> None:
        """反证：不恢复就检测不到 —— 说明 restore 是必需的。"""
        resumed = OscillationDetector()
        report = resumed.record(trace(3, failed=("fmt",), score=60))
        assert report.verdict is OscillationVerdict.NONE

    def test_score_trend_available(self) -> None:
        detector = OscillationDetector()
        for i, score in enumerate([40.0, 60.0, 90.0], start=1):
            detector.record(trace(i, failed=("a",), score=score))
        assert detector.score_trend() == (40.0, 60.0, 90.0)


@pytest.mark.parametrize(
    ("verdict", "terminate", "escalate"),
    [
        (OscillationVerdict.NONE, False, False),
        (OscillationVerdict.ESCALATE, False, True),
        (OscillationVerdict.OSCILLATING, False, True),
        (OscillationVerdict.STALLED, True, False),
    ],
)
def test_verdict_semantics(
    verdict: OscillationVerdict, terminate: bool, escalate: bool
) -> None:
    assert verdict.should_terminate is terminate
    assert verdict.should_escalate is escalate
