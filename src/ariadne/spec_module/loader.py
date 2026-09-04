"""spec.yaml 加载与派生 —— 从声明式 spec 到各模块配置。

spec 是单一事实源（docs/M4 §6）：一份 spec.yaml 派生出
loop_module.Goal（含可验证性校验）和 harness_module.Rule 列表。
加载时即校验，坏 spec 在加载阶段被拒绝（fail-fast）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from ariadne.harness_module.evaluator import HarnessEvaluator
from ariadne.harness_module.loader import compile_rule_set
from ariadne.harness_module.models import (
    Action,
    HookKind,
    Rule,
    RuleCategory,
    Severity,
)
from ariadne.loop_module.goal import (
    Assertion,
    AssertionKind,
    Budget,
    Goal,
    LoopMode,
)
from ariadne.spec_module.schema import (
    AssertionSpec,
    BudgetSpec,
    GoalSpec,
    RuleSpec,
    Spec,
)
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)


class SpecLoadError(ValueError):
    """spec 加载失败。含文件路径与具体错误。"""


class SpecValidationError(ValueError):
    """spec 派生的 Goal 不可验证。"""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


def load_spec(path: Path) -> Spec:
    """从 YAML 文件加载 spec。Pydantic 校验在此时发生。"""
    path = Path(path)
    if not path.is_file():
        raise SpecLoadError(f"spec file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SpecLoadError(f"{path}: YAML parse error: {exc}") from exc
    if raw is None:
        raise SpecLoadError(f"{path}: empty spec file")
    try:
        return Spec.model_validate(raw)
    except ValidationError as exc:
        raise SpecLoadError(f"{path}: schema validation failed:\n{exc}") from exc


def load_spec_from_dict(data: dict[str, Any]) -> Spec:
    """从 dict 加载 spec（API 端直接传 JSON 时用）。"""
    try:
        return Spec.model_validate(data)
    except ValidationError as exc:
        raise SpecLoadError(f"schema validation failed:\n{exc}") from exc


def _derive_assertion(a: AssertionSpec) -> Assertion:
    """AssertionSpec → Assertion。kind 在此校验。"""
    try:
        kind = AssertionKind(a.kind)
    except ValueError as exc:
        raise SpecLoadError(f"unknown assertion kind {a.kind!r}: {exc}") from exc
    return Assertion(
        id=a.id,
        kind=kind,
        spec=a.spec,
        weight=a.weight,
        blocking=a.blocking,
        hint=a.hint,
    )


def _derive_budget(b: BudgetSpec) -> Budget:
    return Budget(
        max_iterations=b.max_iterations,
        max_total_tokens=b.max_total_tokens,
        max_cost_usd=b.max_cost_usd,
        max_tokens_per_iteration=b.max_tokens_per_iteration,
        max_wall_clock_seconds=b.max_wall_clock_seconds,
    )


def derive_goal(spec: Spec) -> Goal:
    """从 spec 派生 loop_module.Goal。

    不做可验证性校验（那是 validate_spec 的职责）。
    只做结构转换：spec.yaml 的 Pydantic 模型 → loop_module 的 frozen dataclass。
    """
    goal_spec: GoalSpec = spec.goal
    assertions = tuple(_derive_assertion(a) for a in goal_spec.assertions)
    budget = _derive_budget(goal_spec.budget)
    mode: LoopMode = goal_spec.mode  # type: ignore[assignment]
    return Goal(
        task=goal_spec.task,
        assertions=assertions,
        budget=budget,
        mode=mode,
        stall_threshold=goal_spec.stall_threshold,
        stall_patience=goal_spec.stall_patience,
    )


def _derive_rule(r: RuleSpec) -> Rule:
    """RuleSpec → harness_module.Rule。枚举值在此校验。"""
    try:
        category = RuleCategory(r.category)
    except ValueError as exc:
        raise SpecLoadError(f"rule {r.id}: unknown category {r.category!r}: {exc}") from exc
    try:
        hook = HookKind(r.hook)
    except ValueError as exc:
        raise SpecLoadError(f"rule {r.id}: unknown hook {r.hook!r}: {exc}") from exc
    try:
        action = Action(r.action)
    except ValueError as exc:
        raise SpecLoadError(f"rule {r.id}: unknown action {r.action!r}: {exc}") from exc
    try:
        severity = Severity(r.severity)
    except ValueError as exc:
        raise SpecLoadError(f"rule {r.id}: unknown severity {r.severity!r}: {exc}") from exc
    return Rule(
        id=r.id,
        category=category,
        hook=hook,
        when=r.when,
        action=action,
        severity=severity,
        message=r.message,
        rewrite_strategy=r.rewrite_strategy,
        route_target=r.route_target,
    )


def derive_rules(spec: Spec) -> list[Rule]:
    """从 spec 派生 harness_module.Rule 列表。"""
    return [_derive_rule(r) for r in spec.rules]


def derive_evaluator(spec: Spec) -> HarnessEvaluator:
    """从 spec 派生编译后的 HarnessEvaluator。

    CEL 语法错误在此暴露（fail-fast）。无规则时返回空求值器（全 ALLOW）。
    """
    rules = derive_rules(spec)
    if not rules:
        return HarnessEvaluator(rules=[])
    return compile_rule_set(rules)


__all__ = [
    "SpecLoadError",
    "SpecValidationError",
    "derive_evaluator",
    "derive_goal",
    "derive_rules",
    "load_spec",
    "load_spec_from_dict",
]
