"""目标可验证性校验。

从 goal.py 拆出：校验规则会随断言类型增长，与数据结构定义分开
更符合项目的单文件 200-400 行约定。

这是整个 Loop 设计的把关点 —— 把"模糊指令"挡在系统之外，
而非等 Loop 跑 10 轮烧完预算才发现目标本身没法判定。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ariadne.eval_module.base import ThresholdOp
from ariadne.loop_module.goal import (
    MAX_ASSERTIONS,
    MAX_ITERATIONS_CAP,
    MIN_ITERATIONS,
    REQUIRED_SPEC_FIELDS,
    Assertion,
    AssertionKind,
    Budget,
    Goal,
)


class GoalValidationError(ValueError):

    """目标不可验证。

    携带逐条原因，便于 API 返回 422 时说明具体哪里不合格。
    """

    def __init__(self, reasons: list[ValidationIssue]) -> None:
        self.reasons = reasons
        detail = "; ".join(f"{r.field}: {r.message}" for r in reasons)
        super().__init__(f"目标不可验证 —— {detail}")


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    message: str
    # warning 不阻止创建，只提示
    severity: Literal["error", "warning"] = "error"


@dataclass(frozen=True)
class ValidationReport:
    issues: tuple[ValidationIssue, ...]

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "error")

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(i for i in self.issues if i.severity == "warning")

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_invalid(self) -> None:
        if self.errors:
            raise GoalValidationError(list(self.errors))


def validate_goal(
    goal: Goal,
    *,
    available_metrics: frozenset[str] = frozenset(),
    sandbox_available: bool = False,
) -> ValidationReport:
    """可验证性校验。这是整个设计的把关点。

    available_metrics 为空时跳过 METRIC 断言的评估器存在性检查 ——
    调用方（API 层）应传入项目已配置的评估器名集合。
    """
    issues: list[ValidationIssue] = []

    if not goal.task.strip():
        issues.append(ValidationIssue("task", "任务描述不能为空"))

    issues.extend(_check_assertions(goal, available_metrics, sandbox_available))
    issues.extend(_check_budget(goal.budget))
    issues.extend(_check_mode_fit(goal))

    return ValidationReport(tuple(issues))


def _check_assertions(
    goal: Goal,
    available_metrics: frozenset[str],
    sandbox_available: bool,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if not goal.assertions:
        issues.append(
            ValidationIssue(
                "assertions",
                "至少需要一条断言。没有可验证条件的目标无法判定是否完成",
            )
        )
        return issues

    if len(goal.assertions) > MAX_ASSERTIONS:
        issues.append(
            ValidationIssue(
                "assertions", f"断言数 {len(goal.assertions)} 超过上限 {MAX_ASSERTIONS}"
            )
        )

    if not goal.blocking_assertions:
        issues.append(
            ValidationIssue(
                "assertions",
                "全部断言都是 blocking=False，缺少硬性收敛条件。"
                "Loop 会一直跑到预算耗尽",
            )
        )

    seen: set[str] = set()
    for assertion in goal.assertions:
        if not assertion.id.strip():
            issues.append(ValidationIssue("assertion.id", "断言 id 不能为空"))
        elif assertion.id in seen:
            issues.append(
                ValidationIssue("assertion.id", f"断言 id 重复: {assertion.id}")
            )
        seen.add(assertion.id)

        if assertion.weight < 0:
            issues.append(
                ValidationIssue(f"assertion[{assertion.id}].weight", "权重不能为负")
            )

        missing = [
            f for f in REQUIRED_SPEC_FIELDS[assertion.kind] if f not in assertion.spec
        ]
        if missing:
            issues.append(
                ValidationIssue(
                    f"assertion[{assertion.id}].spec",
                    f"{assertion.kind.value} 类型缺少必需字段: {', '.join(missing)}",
                )
            )

        issues.extend(
            _check_kind_specific(assertion, available_metrics, sandbox_available)
        )

    # 全是弱信号断言时警告：收敛效率会很差
    if goal.blocking_assertions and all(
        a.signal_strength <= 3 for a in goal.blocking_assertions
    ):
        issues.append(
            ValidationIssue(
                "assertions",
                "全部阻塞性断言都是弱信号（metric 类）。"
                "建议至少一条 command/schema 类断言以提升收敛效率",
                severity="warning",
            )
        )

    return issues


def _check_kind_specific(
    assertion: Assertion,
    available_metrics: frozenset[str],
    sandbox_available: bool,
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    field_name = f"assertion[{assertion.id}].spec"

    if assertion.kind is AssertionKind.COMMAND and not sandbox_available:
        issues.append(
            ValidationIssue(
                field_name,
                "command 类断言需要沙箱/受限执行环境，当前未启用。"
                "请开启执行环境或改用其他断言类型",
            )
        )

    if assertion.kind is AssertionKind.METRIC:
        name = str(assertion.spec.get("name", ""))
        if available_metrics and name not in available_metrics:
            issues.append(
                ValidationIssue(
                    field_name,
                    f"指标 {name!r} 未配置。可用: {', '.join(sorted(available_metrics))}",
                )
            )
        raw_op = assertion.spec.get("op")
        if raw_op is not None and str(raw_op) not in {o.value for o in ThresholdOp}:
            issues.append(
                ValidationIssue(
                    field_name,
                    f"比较符 {raw_op!r} 非法。可用: "
                    f"{', '.join(o.value for o in ThresholdOp)}",
                )
            )
        value = assertion.spec.get("value")
        if value is not None and not isinstance(value, (int, float)):
            issues.append(
                ValidationIssue(field_name, f"阈值必须是数值，收到 {type(value).__name__}")
            )

    if assertion.kind is AssertionKind.REGEX:
        import re

        pattern = str(assertion.spec.get("pattern", ""))
        if pattern:
            try:
                re.compile(pattern)
            except re.error as exc:
                issues.append(
                    ValidationIssue(field_name, f"正则非法: {exc}")
                )

    return issues


def _check_budget(budget: Budget) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if budget.max_iterations < MIN_ITERATIONS:
        issues.append(
            ValidationIssue("budget.max_iterations", f"至少 {MIN_ITERATIONS} 轮")
        )
    if budget.max_iterations > MAX_ITERATIONS_CAP:
        issues.append(
            ValidationIssue(
                "budget.max_iterations",
                f"超过上限 {MAX_ITERATIONS_CAP}。轮次过多通常说明反馈信号无效，"
                "而非需要更多轮次",
            )
        )
    if budget.max_total_tokens <= 0:
        issues.append(ValidationIssue("budget.max_total_tokens", "必须为正"))
    if budget.max_cost_usd <= 0:
        issues.append(ValidationIssue("budget.max_cost_usd", "必须为正"))
    if budget.max_tokens_per_iteration <= 0:
        issues.append(ValidationIssue("budget.max_tokens_per_iteration", "必须为正"))

    # 单轮上限超过总量：第一轮就可能撞穿总预算
    if budget.max_total_tokens < budget.max_tokens_per_iteration:
        issues.append(
            ValidationIssue(
                "budget",
                f"总 Token 上限 {budget.max_total_tokens} 小于单轮上限 "
                f"{budget.max_tokens_per_iteration}，第一轮就会触发熔断",
            )
        )
    if budget.max_wall_clock_seconds <= 0:
        issues.append(ValidationIssue("budget.max_wall_clock_seconds", "必须为正"))

    return issues


def _check_mode_fit(goal: Goal) -> list[ValidationIssue]:
    """模式与断言类型的匹配性检查。"""
    issues: list[ValidationIssue] = []
    kinds = {a.kind for a in goal.assertions}

    if goal.mode == "verify_execute" and AssertionKind.COMMAND not in kinds:
        issues.append(
            ValidationIssue(
                "mode",
                "verify_execute 模式的价值在于用命令退出码作反馈信号，"
                "但断言中没有 command 类型",
                severity="warning",
            )
        )
    if goal.mode == "hitl" and AssertionKind.HUMAN not in kinds:
        issues.append(
            ValidationIssue(
                "mode", "hitl 模式需要至少一条 human 类断言"
            )
        )
    if goal.mode == "retry" and goal.budget.max_iterations > 5:
        issues.append(
            ValidationIssue(
                "budget.max_iterations",
                "retry 模式通常不需要超过 5 轮 —— 重试多次仍失败说明不是暂时性故障",
                severity="warning",
            )
        )
    if goal.stall_patience < 1:
        issues.append(ValidationIssue("stall_patience", "至少为 1"))
    if goal.stall_threshold < 0:
        issues.append(ValidationIssue("stall_threshold", "不能为负"))

    return issues
