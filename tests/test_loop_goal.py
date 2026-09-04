"""目标可验证性校验测试。

这是整个 Loop 设计的把关点：把"模糊指令"挡在系统之外，
而非等 Loop 跑 10 轮烧完预算才发现目标本身没法判定。
"""

from __future__ import annotations

import pytest

from ariadne.loop_module import (
    Assertion,
    AssertionKind,
    Budget,
    Goal,
    GoalValidationError,
    validate_goal,
)


def cmd_assertion(assertion_id: str = "tests", cmd: str = "pytest -q") -> Assertion:
    return Assertion(id=assertion_id, kind=AssertionKind.COMMAND, spec={"cmd": cmd})


def metric_assertion(
    assertion_id: str = "quality",
    *,
    name: str = "composite_quality",
    op: str = ">=",
    value: float = 85.0,
    blocking: bool = True,
) -> Assertion:
    return Assertion(
        id=assertion_id,
        kind=AssertionKind.METRIC,
        spec={"name": name, "op": op, "value": value},
        blocking=blocking,
    )


def goal_with(*assertions: Assertion, **kw: object) -> Goal:
    return Goal(task="写一段说明", assertions=assertions, **kw)  # type: ignore[arg-type]


class TestAcceptsValidGoals:
    def test_command_goal(self) -> None:
        report = validate_goal(
            goal_with(cmd_assertion(), mode="verify_execute"),
            sandbox_available=True,
        )
        assert report.ok
        assert not report.warnings

    def test_metric_goal_with_registered_evaluator(self) -> None:
        report = validate_goal(
            goal_with(metric_assertion()),
            available_metrics=frozenset({"composite_quality"}),
        )
        assert report.ok

    def test_mixed_blocking_and_optional(self) -> None:
        report = validate_goal(
            goal_with(
                cmd_assertion(),
                metric_assertion("lint", name="ruff", blocking=False),
            ),
            available_metrics=frozenset({"ruff"}),
            sandbox_available=True,
        )
        assert report.ok


class TestRejectsUnverifiable:
    def test_empty_assertions(self) -> None:
        """没有可验证条件的目标无法判定是否完成。"""
        report = validate_goal(goal_with())
        assert not report.ok
        assert any("至少需要一条断言" in i.message for i in report.errors)

    def test_all_non_blocking(self) -> None:
        """全 non-blocking 会让 Loop 一直跑到预算耗尽。"""
        report = validate_goal(
            goal_with(metric_assertion(blocking=False)),
            available_metrics=frozenset({"composite_quality"}),
        )
        assert not report.ok
        assert any("缺少硬性收敛条件" in i.message for i in report.errors)

    def test_empty_task(self) -> None:
        report = validate_goal(
            Goal(task="   ", assertions=(cmd_assertion(),)), sandbox_available=True
        )
        assert not report.ok

    def test_unknown_metric_rejected(self) -> None:
        """引用未配置的评估器 —— 运行时才发现就太晚了。"""
        report = validate_goal(
            goal_with(metric_assertion(name="nonexistent")),
            available_metrics=frozenset({"composite_quality"}),
        )
        assert not report.ok
        assert any("未配置" in i.message for i in report.errors)

    def test_command_without_sandbox_rejected(self) -> None:
        """无执行环境时声明 command 断言会在运行时必然失败。"""
        report = validate_goal(goal_with(cmd_assertion()), sandbox_available=False)
        assert not report.ok
        assert any("沙箱" in i.message for i in report.errors)

    def test_missing_spec_fields(self) -> None:
        report = validate_goal(
            goal_with(Assertion(id="bad", kind=AssertionKind.COMMAND, spec={})),
            sandbox_available=True,
        )
        assert not report.ok
        assert any("cmd" in i.message for i in report.errors)

    def test_duplicate_assertion_ids(self) -> None:
        report = validate_goal(
            goal_with(cmd_assertion("same"), cmd_assertion("same")),
            sandbox_available=True,
        )
        assert not report.ok
        assert any("id 重复" in i.message for i in report.errors)

    def test_invalid_regex_rejected(self) -> None:
        report = validate_goal(
            goal_with(
                Assertion(
                    id="re", kind=AssertionKind.REGEX, spec={"pattern": "([unclosed"}
                )
            )
        )
        assert not report.ok
        assert any("正则非法" in i.message for i in report.errors)

    def test_invalid_comparison_operator(self) -> None:
        report = validate_goal(
            goal_with(metric_assertion(op="=~")),
            available_metrics=frozenset({"composite_quality"}),
        )
        assert not report.ok
        assert any("比较符" in i.message for i in report.errors)

    def test_non_numeric_threshold(self) -> None:
        report = validate_goal(
            goal_with(
                Assertion(
                    id="q",
                    kind=AssertionKind.METRIC,
                    spec={"name": "q", "op": ">=", "value": "high"},
                )
            ),
            available_metrics=frozenset({"q"}),
        )
        assert not report.ok
        assert any("必须是数值" in i.message for i in report.errors)

    def test_negative_weight(self) -> None:
        report = validate_goal(
            goal_with(
                Assertion(
                    id="w", kind=AssertionKind.COMMAND, spec={"cmd": "x"}, weight=-1.0
                )
            ),
            sandbox_available=True,
        )
        assert not report.ok


class TestBudgetValidation:
    def test_per_iteration_exceeding_total_rejected(self) -> None:
        """第一轮就会触发熔断 —— 这种配置必然跑不起来。"""
        report = validate_goal(
            goal_with(
                cmd_assertion(),
                budget=Budget(max_total_tokens=1000, max_tokens_per_iteration=32_000),
            ),
            sandbox_available=True,
        )
        assert not report.ok
        assert any("第一轮就会触发熔断" in i.message for i in report.errors)

    @pytest.mark.parametrize(
        "budget",
        [
            Budget(max_iterations=0),
            Budget(max_iterations=100),
            Budget(max_total_tokens=0),
            Budget(max_cost_usd=0.0),
            Budget(max_tokens_per_iteration=0),
            Budget(max_wall_clock_seconds=0),
        ],
    )
    def test_invalid_budgets_rejected(self, budget: Budget) -> None:
        report = validate_goal(
            goal_with(cmd_assertion(), budget=budget), sandbox_available=True
        )
        assert not report.ok

    def test_iteration_cap_message_explains_why(self) -> None:
        """轮次过多通常说明反馈信号无效，而非需要更多轮次。"""
        report = validate_goal(
            goal_with(cmd_assertion(), budget=Budget(max_iterations=100)),
            sandbox_available=True,
        )
        assert any("反馈信号无效" in i.message for i in report.errors)


class TestWarnings:
    def test_all_weak_signals_warns_not_blocks(self) -> None:
        """全弱信号断言收敛效率差，但不该直接拒绝 —— 有些场景只能用 metric。"""
        report = validate_goal(
            goal_with(metric_assertion(), metric_assertion("m2", name="ifr")),
            available_metrics=frozenset({"composite_quality", "ifr"}),
        )
        assert report.ok
        assert any("弱信号" in i.message for i in report.warnings)

    def test_verify_execute_without_command_warns(self) -> None:
        report = validate_goal(
            goal_with(metric_assertion(), mode="verify_execute"),
            available_metrics=frozenset({"composite_quality"}),
        )
        assert report.ok
        assert any("command" in i.message for i in report.warnings)

    def test_retry_with_many_iterations_warns(self) -> None:
        """重试多次仍失败说明不是暂时性故障。"""
        report = validate_goal(
            goal_with(
                Assertion(id="s", kind=AssertionKind.SCHEMA, spec={"schema": {}}),
                mode="retry",
                budget=Budget(max_iterations=20),
            )
        )
        assert report.ok
        assert any("暂时性故障" in i.message for i in report.warnings)

    def test_hitl_without_human_assertion_is_error(self) -> None:
        """这个不是警告而是错误：hitl 模式没有 human 断言就没有审批点。"""
        report = validate_goal(
            goal_with(cmd_assertion(), mode="hitl"), sandbox_available=True
        )
        assert not report.ok


class TestGoalHelpers:
    def test_blocking_ids(self) -> None:
        goal = goal_with(
            cmd_assertion("a"),
            metric_assertion("b", blocking=False),
            cmd_assertion("c"),
        )
        assert goal.blocking_ids == {"a", "c"}

    def test_spec_summary_separates_blocking(self) -> None:
        """给模型看的规格必须区分"必须"与"期望"。"""
        goal = goal_with(
            cmd_assertion("must"),
            metric_assertion("nice", blocking=False),
        )
        summary = goal.spec_summary()
        assert "必须满足的条件" in summary
        assert "期望满足（不阻塞完成）" in summary
        assert summary.index("必须满足") < summary.index("期望满足")

    def test_spec_summary_omits_optional_section_when_none(self) -> None:
        summary = goal_with(cmd_assertion()).spec_summary()
        assert "期望满足" not in summary

    def test_describe_hides_internal_spec(self) -> None:
        """给模型的描述是人话，不暴露 spec 结构。"""
        described = metric_assertion().describe()
        assert "composite_quality" in described
        assert "{" not in described

    def test_signal_strength_ordering(self) -> None:
        """command/schema 强于 regex 强于 metric —— 决定收敛效率。"""
        assert cmd_assertion().signal_strength > metric_assertion().signal_strength

    def test_assertion_by_id(self) -> None:
        goal = goal_with(cmd_assertion("target"))
        assert goal.assertion_by_id("target") is not None
        assert goal.assertion_by_id("nope") is None


class TestErrorRaising:
    def test_raise_if_invalid_carries_reasons(self) -> None:
        report = validate_goal(goal_with())
        with pytest.raises(GoalValidationError) as excinfo:
            report.raise_if_invalid()
        assert excinfo.value.reasons
        assert "assertions" in str(excinfo.value)

    def test_raise_if_invalid_silent_when_ok(self) -> None:
        report = validate_goal(goal_with(cmd_assertion()), sandbox_available=True)
        report.raise_if_invalid()

    def test_warnings_do_not_raise(self) -> None:
        report = validate_goal(
            goal_with(metric_assertion()),
            available_metrics=frozenset({"composite_quality"}),
        )
        assert report.warnings
        report.raise_if_invalid()
