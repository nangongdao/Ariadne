"""Critique 与上下文收敛测试。

两个核心命题：
1. critique 是结构化指令，不是把 eval JSON 原样塞回去
2. 上下文不随轮次膨胀 —— 10 轮的单轮上下文量应基本持平
"""

from __future__ import annotations

from ariadne.loop_module.context import (
    ContextBuilder,
    OutputMode,
    make_diff,
)
from ariadne.loop_module.critique import (
    Critique,
    CritiqueSynthesizer,
    summarize_history,
)
from ariadne.loop_module.fingerprint import (
    OscillationReport,
    OscillationVerdict,
)
from ariadne.loop_module.goal import Assertion, AssertionKind, Budget, Goal
from ariadne.loop_module.verifier.base import AssertionOutcome, Verdict
from ariadne.utils.tokens import estimate_tokens


def assertion(
    assertion_id: str,
    kind: AssertionKind = AssertionKind.COMMAND,
    *,
    hint: str = "",
    **spec: object,
) -> Assertion:
    return Assertion(id=assertion_id, kind=kind, spec=dict(spec), hint=hint)


def goal_of(*assertions: Assertion, **kw: object) -> Goal:
    return Goal(task="修复失败的测试", assertions=assertions, **kw)  # type: ignore[arg-type]


def failed_outcome(
    assertion_id: str,
    *,
    kind: AssertionKind = AssertionKind.COMMAND,
    evidence: str = "",
    errored: bool = False,
) -> AssertionOutcome:
    return AssertionOutcome(
        assertion_id=assertion_id,
        kind=kind,
        passed=False,
        evidence=evidence,
        errored=errored,
    )


def verdict_of(*outcomes: AssertionOutcome) -> Verdict:
    failed = tuple(o for o in outcomes if not o.passed and not o.pending_human)
    return Verdict(
        converged=False,
        failed=failed,
        errored=tuple(o.assertion_id for o in outcomes if o.errored),
        outcomes=outcomes,
        score=40.0,
    )


class TestCritiqueGeneration:
    def test_uses_human_hint_when_available(self) -> None:
        """人工预设的提示效果通常远好于模板 —— 必须优先。"""
        goal = goal_of(
            assertion("tests", hint="先修 parse_date 的时区处理", cmd="pytest")
        )
        critique = CritiqueSynthesizer().synthesize(
            verdict_of(failed_outcome("tests")), goal
        )
        assert "先修 parse_date 的时区处理" in critique.directives

    def test_falls_back_to_template(self) -> None:
        goal = goal_of(assertion("tests", cmd="pytest"))
        critique = CritiqueSynthesizer().synthesize(
            verdict_of(failed_outcome("tests")), goal
        )
        assert critique.directives
        assert "定位根因" in critique.directives[0]

    def test_failures_are_human_readable_not_ids(self) -> None:
        """描述给模型看，不该暴露内部 assertion id。"""
        goal = goal_of(assertion("a1b2c3", cmd="pytest -q"))
        critique = CritiqueSynthesizer().synthesize(
            verdict_of(failed_outcome("a1b2c3")), goal
        )
        assert critique.failures
        assert "a1b2c3" not in critique.failures[0]
        assert "pytest -q" in critique.failures[0]

    def test_evidence_truncated(self) -> None:
        """一个完整 stack trace 能吃掉整个上下文预算。"""
        long_trace = "\n".join(f"  File line {i}" for i in range(500))
        critique = CritiqueSynthesizer().synthesize(
            verdict_of(failed_outcome("tests", evidence=long_trace)),
            goal_of(assertion("tests", cmd="pytest")),
        )
        assert critique.evidence
        assert "省略" in critique.evidence[0]
        assert len(critique.evidence[0]) < len(long_trace)

    def test_errored_assertions_get_no_directive(self) -> None:
        """errored 是配置/环境问题，让模型去"修"是无效指令。"""
        goal = goal_of(assertion("broken", cmd="nonexistent"))
        critique = CritiqueSynthesizer().synthesize(
            verdict_of(failed_outcome("broken", errored=True)), goal
        )
        assert not critique.directives
        # 但要说明有检查没跑成，而非静默忽略
        assert any("未能执行" in f for f in critique.failures)

    def test_duplicate_directives_deduplicated(self) -> None:
        """两条同类断言不该产生两条相同指令。"""
        goal = goal_of(
            assertion("t1", cmd="pytest a"), assertion("t2", cmd="pytest b")
        )
        critique = CritiqueSynthesizer().synthesize(
            verdict_of(failed_outcome("t1"), failed_outcome("t2")), goal
        )
        assert len(critique.directives) == 1

    def test_failures_capped(self) -> None:
        goal = goal_of(*[assertion(f"t{i}", cmd=f"cmd{i}") for i in range(20)])
        critique = CritiqueSynthesizer(max_failures=3).synthesize(
            verdict_of(*[failed_outcome(f"t{i}") for i in range(20)]), goal
        )
        assert len(critique.failures) == 3

    def test_empty_when_nothing_failed(self) -> None:
        critique = CritiqueSynthesizer().synthesize(
            Verdict(converged=True, passed=("a",)), goal_of(assertion("a"))
        )
        assert critique.is_empty
        assert critique.render() == ""


class TestForbiddenAndEscalation:
    def test_forbidden_from_oscillation(self) -> None:
        """把"这些路走过了"显式列出是防振荡的关键。"""
        report = OscillationReport(
            verdict=OscillationVerdict.ESCALATE,
            forbid_hints=("轮次 1：fmt 未通过", "轮次 2：fmt 未通过"),
        )
        critique = CritiqueSynthesizer().synthesize(
            verdict_of(failed_outcome("fmt")),
            goal_of(assertion("fmt")),
            oscillation=report,
        )
        assert len(critique.forbidden) == 2
        assert "已尝试且失败的方向" in critique.render()

    def test_escalation_message_for_oscillating(self) -> None:
        report = OscillationReport(verdict=OscillationVerdict.OSCILLATING)
        critique = CritiqueSynthesizer().synthesize(
            verdict_of(failed_outcome("fmt")),
            goal_of(assertion("fmt")),
            oscillation=report,
        )
        assert "不同的实现思路" in critique.escalation

    def test_no_escalation_when_clean(self) -> None:
        report = OscillationReport(verdict=OscillationVerdict.NONE)
        critique = CritiqueSynthesizer().synthesize(
            verdict_of(failed_outcome("fmt")),
            goal_of(assertion("fmt")),
            oscillation=report,
        )
        assert critique.escalation == ""

    def test_forbidden_capped(self) -> None:
        report = OscillationReport(
            verdict=OscillationVerdict.ESCALATE,
            forbid_hints=tuple(f"轮次 {i}" for i in range(20)),
        )
        critique = CritiqueSynthesizer(max_forbidden=3).synthesize(
            verdict_of(failed_outcome("f")), goal_of(assertion("f")), oscillation=report
        )
        assert len(critique.forbidden) == 3


class TestRendering:
    def test_sections_present(self) -> None:
        critique = Critique(
            failures=("检查 A 失败",),
            evidence=("报错细节",),
            directives=("改这里",),
            forbidden=("试过 B",),
            escalation="换解法",
        )
        text = critique.render()
        assert "## 未通过的检查" in text
        assert "## 具体证据" in text
        assert "## 本轮需要做的修正" in text
        assert "## 已尝试且失败的方向" in text
        assert "## 注意" in text

    def test_sections_omitted_when_empty(self) -> None:
        critique = Critique(failures=("A",), directives=("B",))
        text = critique.render()
        assert "## 具体证据" not in text
        assert "## 已尝试" not in text


class TestHistorySummary:
    def test_only_failures_kept(self) -> None:
        history = [
            Critique(failures=("格式错误",)),
            Critique(),
            Critique(failures=("引用缺失",)),
        ]
        summary = summarize_history(history)
        assert "格式错误" in summary
        assert "引用缺失" in summary
        assert summary.count("轮次") == 2

    def test_secondary_compression(self) -> None:
        """超出上限时早期轮次合并为一行 —— 避免历史段无限膨胀。"""
        history = [Critique(failures=(f"问题{i}",)) for i in range(10)]
        summary = summarize_history(history, max_lines=3)
        assert "省略" in summary
        assert summary.count("轮次") <= 3


class TestContextConvergence:
    @staticmethod
    def builder(**kw: object) -> ContextBuilder:
        goal = goal_of(
            assertion("tests", cmd="pytest -q"),
            budget=Budget(max_tokens_per_iteration=4000),
        )
        return ContextBuilder(goal=goal, **kw)  # type: ignore[arg-type]

    def test_four_segments(self) -> None:
        segments = self.builder().build(
            iteration=2,
            last_output="输出内容",
            critique=Critique(failures=("A",), directives=("改 A",)),
            history=[Critique(failures=("旧问题",))],
        )
        breakdown = segments.token_breakdown()
        assert all(k in breakdown for k in ("spec", "last_output", "critique", "history"))
        assert breakdown["spec"] > 0

    def test_spec_first_for_cache_hit(self) -> None:
        """固定段放最前面才能命中 provider 的 prompt 缓存。"""
        segments = self.builder().build(iteration=1, last_output="x")
        rendered = segments.render()
        assert rendered.startswith(segments.spec[:20])

    def test_does_not_grow_with_iterations(self) -> None:
        """**核心约束**：10 轮的单轮上下文量应基本持平。"""
        builder = self.builder()
        sizes: list[int] = []
        history: list[Critique] = []

        for iteration in range(1, 11):
            history.append(Critique(failures=(f"问题 {iteration}" * 20,)))
            segments = builder.build(
                iteration=iteration,
                last_output="模型输出" * 100,
                critique=Critique(
                    failures=("当前问题",), directives=("改这里",)
                ),
                history=history,
            )
            sizes.append(segments.total_tokens)

        # 后期不应比早期显著膨胀（允许 1.5 倍以内的波动）
        assert sizes[-1] < sizes[1] * 1.5, f"上下文随轮次膨胀: {sizes}"

    def test_respects_budget_ratio(self) -> None:
        """上下文不超单轮预算的 60%，剩余留给输出。"""
        builder = self.builder()
        segments = builder.build(
            iteration=3,
            last_output="超长输出" * 5000,
            critique=Critique(failures=("A",), directives=("B",)),
            history=[Critique(failures=("旧",))] * 10,
        )
        assert segments.total_tokens <= 4000 * 0.6 + 50

    def test_spec_and_critique_never_dropped(self) -> None:
        """砍掉任一个都会让这一轮变成瞎猜。"""
        builder = self.builder()
        segments = builder.build(
            iteration=3,
            last_output="巨量输出" * 20000,
            critique=Critique(failures=("关键问题",), directives=("必须做的修正",)),
            history=[Critique(failures=("旧",))] * 20,
        )
        assert segments.spec
        assert "必须做的修正" in segments.critique

    def test_history_dropped_before_output(self) -> None:
        """裁剪顺序：历史优先被砍，上一轮输出尽量保留。"""
        # 预算收紧到刚好容不下"spec + 输出 + 历史"（≈396），
        # 但容得下"spec + 输出 + critique"（≈134）
        goal = goal_of(
            assertion("tests", cmd="pytest -q"),
            budget=Budget(max_tokens_per_iteration=500),
        )
        builder = ContextBuilder(goal=goal)
        segments = builder.build(
            iteration=3,
            last_output="中等长度输出" * 40,
            critique=Critique(failures=("A",), directives=("B",)),
            history=[Critique(failures=("旧问题" * 40,))] * 8,
        )
        assert segments.history == "", "历史应被优先裁掉"
        assert segments.last_output, "上一轮输出应保留"
        assert "省略" not in segments.last_output, "砍掉历史后输出无需再截断"


class TestDiffMode:
    def test_diff_shorter_than_full_for_small_change(self) -> None:
        """改一行的大文件用 diff 只有几行，全文会占满上下文。"""
        previous = "\n".join(f"line {i}" for i in range(500))
        current = previous.replace("line 250", "line 250 modified")

        goal = goal_of(
            assertion("t", cmd="pytest"),
            budget=Budget(max_tokens_per_iteration=100_000),
        )
        diff_builder = ContextBuilder(goal=goal, output_mode=OutputMode.DIFF)
        full_builder = ContextBuilder(goal=goal, output_mode=OutputMode.FULL)

        diff_ctx = diff_builder.build(
            iteration=2, last_output=current, previous_output=previous
        )
        full_ctx = full_builder.build(iteration=2, last_output=current)

        assert diff_ctx.token_breakdown()["last_output"] < (
            full_ctx.token_breakdown()["last_output"] / 5
        )

    def test_diff_falls_back_to_full_when_unchanged(self) -> None:
        """输出没变时 diff 为空，给全文更有用。"""
        goal = goal_of(assertion("t", cmd="pytest"))
        builder = ContextBuilder(goal=goal, output_mode=OutputMode.DIFF)
        segments = builder.build(
            iteration=2, last_output="same", previous_output="same"
        )
        assert "上一轮输出" in segments.last_output

    def test_make_diff_marks_changes(self) -> None:
        diff = make_diff("a\nb\nc", "a\nB\nc")
        assert "-b" in diff
        assert "+B" in diff


def test_estimate_tokens_is_conservative() -> None:
    """宁可高估 —— 低估会导致超支。"""
    text = "hello world " * 100
    # 实际 Token 约 200，估算应不低于此
    assert estimate_tokens(text) >= 200
