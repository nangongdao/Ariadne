"""Ralph Verifier 测试。

M3 最关键的测试文件。核心命题：**收敛判定与模型自评完全无关**。
早期 Agent 最致命的缺陷是"模型说完成了就停了，但任务远未达标"，
这里逐条验证该缺陷不会重现。
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from ariadne.loop_module.goal import Assertion, AssertionKind, Goal
from ariadne.loop_module.verifier import (
    AssertionOutcome,
    VerificationContext,
    VerifierFactory,
    available_kinds,
    compute_score,
    judge,
)
from ariadne.loop_module.verifier.builtin import (
    DictMetricProvider,
    InMemoryApprovals,
)


def assertion(
    assertion_id: str,
    kind: AssertionKind = AssertionKind.REGEX,
    *,
    blocking: bool = True,
    weight: float = 1.0,
    **spec: object,
) -> Assertion:
    return Assertion(
        id=assertion_id,
        kind=kind,
        spec=dict(spec),
        blocking=blocking,
        weight=weight,
    )


def goal_of(*assertions: Assertion) -> Goal:
    return Goal(task="任务", assertions=assertions)


def outcome(
    assertion_id: str,
    *,
    passed: bool,
    value: float | None = None,
    errored: bool = False,
    pending: bool = False,
) -> AssertionOutcome:
    return AssertionOutcome(
        assertion_id=assertion_id,
        kind=AssertionKind.REGEX,
        passed=passed,
        value=value if value is not None else (1.0 if passed else 0.0),
        errored=errored,
        pending_human=pending,
    )


class TestRalphPrinciple:
    """核心：收敛只看 blocking 断言，与模型自评无关。"""

    def test_claimed_done_does_not_make_converged(self) -> None:
        """模型自称完成但断言未过 → 不收敛。这是 Ralph 原则的全部要点。"""
        goal = goal_of(assertion("must"))
        verdict = judge(
            (outcome("must", passed=False),), goal, claimed_done=True
        )
        assert not verdict.converged
        assert verdict.claimed_done
        assert verdict.false_completion

    def test_converged_without_claiming(self) -> None:
        """反过来也成立：模型没说完成，但断言全过 → 收敛。"""
        goal = goal_of(assertion("must"))
        verdict = judge((outcome("must", passed=True),), goal, claimed_done=False)
        assert verdict.converged
        assert not verdict.false_completion

    def test_high_score_does_not_override_failed_blocking(self) -> None:
        """高分不能掩盖硬性失败 —— 否则又回到"分数可被讨好"的老问题。"""
        goal = goal_of(
            assertion("must"), assertion("nice", blocking=False)
        )
        verdict = judge(
            (
                outcome("must", passed=False, value=0.95),
                outcome("nice", passed=True, value=1.0),
            ),
            goal,
        )
        assert verdict.score > 90, "得分确实很高"
        assert not verdict.converged, "但仍不收敛"

    def test_non_blocking_failure_does_not_prevent_convergence(self) -> None:
        """non-blocking 断言失败不阻塞收敛 —— 用于 κ 不达标的 Judge 指标。"""
        goal = goal_of(
            assertion("must"), assertion("optional", blocking=False)
        )
        verdict = judge(
            (
                outcome("must", passed=True),
                outcome("optional", passed=False),
            ),
            goal,
        )
        assert verdict.converged

    def test_errored_assertion_is_not_passed(self) -> None:
        """"没测出来"不能当"通过"——那是掩盖故障。"""
        goal = goal_of(assertion("must"))
        verdict = judge(
            (outcome("must", passed=False, errored=True),), goal
        )
        assert not verdict.converged
        assert verdict.errored == ("must",)

    def test_false_completion_requires_both_conditions(self) -> None:
        goal = goal_of(assertion("a"))
        # 自称完成 + 已收敛 → 不是假完成
        assert not judge(
            (outcome("a", passed=True),), goal, claimed_done=True
        ).false_completion
        # 未自称 + 未收敛 → 也不是假完成（只是还没做完）
        assert not judge(
            (outcome("a", passed=False),), goal, claimed_done=False
        ).false_completion


class TestScoring:
    def test_score_is_weighted(self) -> None:
        goal = goal_of(
            assertion("heavy", weight=3.0), assertion("light", weight=1.0)
        )
        verdict = judge(
            (
                outcome("heavy", passed=True, value=1.0),
                outcome("light", passed=False, value=0.0),
            ),
            goal,
        )
        assert verdict.score == 75.0

    def test_errored_excluded_from_score_denominator(self) -> None:
        """出错项既不加分也不扣分 —— 否则"没测出来"被当"很差"，
        会让 Loop 朝错误方向修正。"""
        goal = goal_of(assertion("ok"), assertion("broken"))
        verdict = judge(
            (
                outcome("ok", passed=True, value=1.0),
                outcome("broken", passed=False, value=0.0, errored=True),
            ),
            goal,
        )
        assert verdict.score == 100.0

    def test_all_errored_scores_zero(self) -> None:
        goal = goal_of(assertion("a"))
        verdict = judge((outcome("a", passed=False, errored=True),), goal)
        assert verdict.score == 0.0

    def test_score_with_no_outcomes(self) -> None:
        assert compute_score((), goal_of(assertion("a"))) == 0.0

    def test_unknown_assertion_uses_default_weight(self) -> None:
        """结果里出现 goal 中没有的断言 id 时不该崩。"""
        goal = goal_of(assertion("known"))
        score = compute_score(
            (
                outcome("known", passed=True, value=1.0),
                outcome("stray", passed=False, value=0.0),
            ),
            goal,
        )
        assert score == 50.0


class TestPendingHuman:
    def test_pending_not_counted_as_failure(self) -> None:
        """等待审批不是失败 —— 不该出现在 failed 里让 critique 去"修"。"""
        goal = goal_of(assertion("approval", AssertionKind.HUMAN))
        verdict = judge((outcome("approval", passed=False, pending=True),), goal)
        assert verdict.pending_human == ("approval",)
        assert verdict.failed == ()
        assert verdict.needs_human
        assert not verdict.converged


class TestSchemaVerifier:
    SCHEMA: ClassVar[dict[str, object]] = {
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }

    def test_valid_json_passes(self) -> None:
        verifier = VerifierFactory(AssertionKind.SCHEMA)
        result = verifier.verify(
            assertion("s", AssertionKind.SCHEMA, schema=self.SCHEMA),
            VerificationContext(output='{"name":"a"}'),
        )
        assert result.passed

    def test_missing_field_fails_with_evidence(self) -> None:
        verifier = VerifierFactory(AssertionKind.SCHEMA)
        result = verifier.verify(
            assertion("s", AssertionKind.SCHEMA, schema=self.SCHEMA),
            VerificationContext(output="{}"),
        )
        assert not result.passed
        assert "name" in result.evidence

    def test_bad_spec_is_errored_not_failed(self) -> None:
        """spec 写错是配置问题，不是输出不合格。"""
        verifier = VerifierFactory(AssertionKind.SCHEMA)
        result = verifier.verify(
            assertion("s", AssertionKind.SCHEMA, schema="not-a-dict"),
            VerificationContext(output="{}"),
        )
        assert result.errored


class TestRegexVerifier:
    def test_must_match(self) -> None:
        verifier = VerifierFactory(AssertionKind.REGEX)
        ctx = VerificationContext(output="# 标题\n正文")
        assert verifier.verify(
            assertion("r", AssertionKind.REGEX, pattern=r"^#\s"), ctx
        ).passed

    def test_must_not_match(self) -> None:
        verifier = VerifierFactory(AssertionKind.REGEX)
        result = verifier.verify(
            assertion("r", AssertionKind.REGEX, pattern="TODO", must_match=False),
            VerificationContext(output="有 TODO 残留"),
        )
        assert not result.passed
        assert "TODO" in result.evidence

    def test_invalid_regex_is_errored(self) -> None:
        verifier = VerifierFactory(AssertionKind.REGEX)
        result = verifier.verify(
            assertion("r", AssertionKind.REGEX, pattern="([unclosed"),
            VerificationContext(output="x"),
        )
        assert result.errored


class TestMetricVerifier:
    def test_threshold_met(self) -> None:
        verifier = VerifierFactory(
            AssertionKind.METRIC,
            provider=DictMetricProvider({"quality": 90.0}),
        )
        result = verifier.verify(
            assertion("m", AssertionKind.METRIC, name="quality", op=">=", value=85),
            VerificationContext(output="x"),
        )
        assert result.passed
        assert result.value == 1.0

    def test_threshold_missed_reports_actual(self) -> None:
        verifier = VerifierFactory(
            AssertionKind.METRIC, provider=DictMetricProvider({"quality": 70.0})
        )
        result = verifier.verify(
            assertion("m", AssertionKind.METRIC, name="quality", op=">=", value=85),
            VerificationContext(output="x"),
        )
        assert not result.passed
        assert "70" in result.evidence
        # 部分得分用于趋势图：70/85 ≈ 0.82
        assert 0.8 < result.value < 0.85

    def test_missing_metric_is_errored_not_failed(self) -> None:
        """指标拿不到是配置问题。判失败会让 critique 给出"提升该指标"的无效指令。"""
        verifier = VerifierFactory(
            AssertionKind.METRIC, provider=DictMetricProvider({})
        )
        result = verifier.verify(
            assertion("m", AssertionKind.METRIC, name="absent", op=">=", value=1),
            VerificationContext(output="x"),
        )
        assert result.errored
        assert "不可用" in result.evidence

    def test_invalid_operator_is_errored(self) -> None:
        verifier = VerifierFactory(
            AssertionKind.METRIC, provider=DictMetricProvider({"q": 1.0})
        )
        result = verifier.verify(
            assertion("m", AssertionKind.METRIC, name="q", op="=~", value=1),
            VerificationContext(output="x"),
        )
        assert result.errored

    def test_lower_is_better_operator(self) -> None:
        verifier = VerifierFactory(
            AssertionKind.METRIC, provider=DictMetricProvider({"latency": 100.0})
        )
        result = verifier.verify(
            assertion("m", AssertionKind.METRIC, name="latency", op="<=", value=200),
            VerificationContext(output="x"),
        )
        assert result.passed


class TestHumanVerifier:
    def test_undecided_is_pending(self) -> None:
        verifier = VerifierFactory(
            AssertionKind.HUMAN, store=InMemoryApprovals()
        )
        result = verifier.verify(
            assertion("h", AssertionKind.HUMAN), VerificationContext(output="x")
        )
        assert result.pending_human
        assert not result.passed

    def test_approved_passes(self) -> None:
        approvals = InMemoryApprovals()
        approvals.approve("h")
        verifier = VerifierFactory(AssertionKind.HUMAN, store=approvals)
        result = verifier.verify(
            assertion("h", AssertionKind.HUMAN), VerificationContext(output="x")
        )
        assert result.passed
        assert not result.pending_human

    def test_rejected_fails_without_pending(self) -> None:
        approvals = InMemoryApprovals()
        approvals.reject("h")
        verifier = VerifierFactory(AssertionKind.HUMAN, store=approvals)
        result = verifier.verify(
            assertion("h", AssertionKind.HUMAN), VerificationContext(output="x")
        )
        assert not result.passed
        assert not result.pending_human


class TestRegistry:
    def test_all_five_kinds_registered(self) -> None:
        assert len(available_kinds()) == 5

    def test_unknown_kind_raises(self) -> None:
        from ariadne.loop_module.verifier import VERIFIER_REGISTRY

        saved = VERIFIER_REGISTRY.pop(AssertionKind.REGEX)
        try:
            with pytest.raises(ValueError, match="未注册的断言类型"):
                VerifierFactory(AssertionKind.REGEX)
        finally:
            VERIFIER_REGISTRY[AssertionKind.REGEX] = saved

    def test_verify_never_raises(self) -> None:
        """契约：单条断言验证失败不能让整轮裁决崩掉。"""
        verifier = VerifierFactory(AssertionKind.SCHEMA)
        # spec 完全缺失
        result = verifier.verify(
            Assertion(id="broken", kind=AssertionKind.SCHEMA),
            VerificationContext(output="x"),
        )
        assert result.errored
        assert not result.passed

    def test_duration_recorded(self) -> None:
        verifier = VerifierFactory(AssertionKind.REGEX)
        result = verifier.verify(
            assertion("r", AssertionKind.REGEX, pattern="x"),
            VerificationContext(output="x"),
        )
        assert result.duration_ms >= 0
