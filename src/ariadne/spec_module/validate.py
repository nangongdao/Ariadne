"""spec 可验证性校验 —— 复用 goal_validation 的逻辑。

这是 spec 作为单一事实源的最后一道关口（docs/M4 §6 validate.py）：
空断言、全 non-blocking、未知评估器、无沙箱声明 command → 拒绝。
"""

from __future__ import annotations

from ariadne.loop_module.goal_validation import (
    GoalValidationError,
    ValidationReport,
    validate_goal,
)
from ariadne.spec_module.loader import derive_goal
from ariadne.spec_module.schema import Spec


def validate_spec(
    spec: Spec,
    *,
    available_metrics: frozenset[str] = frozenset(),
    sandbox_available: bool = False,
) -> ValidationReport:
    """校验 spec 派生的 Goal 是否可验证。

    参数与 goal_validation.validate_goal 一致：
    - available_metrics: 项目已配置的评估器名集合（METRIC 断言用）
    - sandbox_available: 沙箱是否可用（COMMAND 断言需要）
    """
    goal = derive_goal(spec)
    return validate_goal(
        goal,
        available_metrics=available_metrics,
        sandbox_available=sandbox_available,
    )


def validate_spec_or_raise(
    spec: Spec,
    *,
    available_metrics: frozenset[str] = frozenset(),
    sandbox_available: bool = False,
) -> None:
    """校验 spec，不可验证时抛 GoalValidationError。"""
    report = validate_spec(
        spec,
        available_metrics=available_metrics,
        sandbox_available=sandbox_available,
    )
    if not report.ok:
        raise GoalValidationError(list(report.errors))


__all__ = [
    "validate_spec",
    "validate_spec_or_raise",
]
