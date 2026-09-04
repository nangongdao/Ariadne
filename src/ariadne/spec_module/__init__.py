"""Spec 模块 —— spec.yaml 驱动的单一事实源。

一份 spec.yaml 派生出：
- loop_module.Goal（含可验证性校验）
- harness_module.Rule 列表 → HarnessEvaluator
- sandbox_module 配置

不写 spec 时用默认值（最小安全默认值）。
"""

from ariadne.spec_module.loader import (
    SpecLoadError,
    SpecValidationError,
    derive_evaluator,
    derive_goal,
    derive_rules,
    load_spec,
    load_spec_from_dict,
)
from ariadne.spec_module.schema import (
    AssertionSpec,
    BudgetSpec,
    GoalSpec,
    RuleSpec,
    SandboxSpec,
    Spec,
)
from ariadne.spec_module.validate import validate_spec, validate_spec_or_raise

__all__ = [
    "AssertionSpec",
    "BudgetSpec",
    "GoalSpec",
    "RuleSpec",
    "SandboxSpec",
    "Spec",
    "SpecLoadError",
    "SpecValidationError",
    "derive_evaluator",
    "derive_goal",
    "derive_rules",
    "load_spec",
    "load_spec_from_dict",
    "validate_spec",
    "validate_spec_or_raise",
]
