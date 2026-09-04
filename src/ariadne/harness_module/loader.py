"""Harness 规则集加载 —— YAML 文件 → Rule 列表 → 编译后的 HarnessEvaluator。

加载时即校验 schema 与 CEL 语法，坏规则在加载阶段就被拒绝（fail-fast），
不会把坏规则带入运行时。这是安全边界的一部分：一条 CEL 语法错的规则
如果到运行时才发现，fail-closed 会把它当命中（拒绝），导致合法流量被拦。

加载顺序遵循 rule_sort_key（确定性排序），保证同一规则集多次加载的
规则顺序一致 —— 审计要求"同样的规则集得到同样的裁决"。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ariadne.harness_module.evaluator import (
    EvaluationError,
    HarnessEvaluator,
    compile_rule_with_functions,
)
from ariadne.harness_module.models import (
    Action,
    HookKind,
    Rule,
    RuleCategory,
    Severity,
)
from ariadne.harness_module.resolve import rule_sort_key
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)


class RuleYAML(BaseModel):
    """单条规则的 YAML schema 校验模型。

    Pydantic 校验确保字段类型合法、枚举值在范围内。CEL 表达式的语法
    校验在编译阶段（compile）做，不在此处 —— 这样校验分两层：
    schema 层（快、无依赖）和 CEL 层（需要 celpy）。
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    category: RuleCategory
    hook: HookKind
    when: str = Field(min_length=1)
    action: Action = Action.WARN  # 验收项 13：缺省 warn
    severity: Severity = Severity.MEDIUM
    message: str = ""
    rewrite_strategy: str = ""
    route_target: str = ""

    @field_validator("id")
    @classmethod
    def _id_no_whitespace(cls, v: str) -> str:
        if any(c.isspace() for c in v):
            raise ValueError("rule id must not contain whitespace")
        return v

    def to_rule(self) -> Rule:
        return Rule(
            id=self.id,
            category=self.category,
            hook=self.hook,
            when=self.when,
            action=self.action,
            severity=self.severity,
            message=self.message,
            rewrite_strategy=self.rewrite_strategy,
            route_target=self.route_target,
        )


class RuleLoadError(ValueError):
    """规则集加载失败。含文件路径与具体错误，便于定位。"""


def _parse_rule_dicts(data: Any, source: str) -> list[Rule]:
    """把 YAML 解析出的原始 dict 列表校验并转为 Rule 列表。"""
    if isinstance(data, dict) and "rules" in data:
        raw_rules = data["rules"]
        if not isinstance(raw_rules, list):
            raise RuleLoadError(f"{source}: 'rules' must be a list")
    elif isinstance(data, list):
        raw_rules = data
    else:
        raise RuleLoadError(f"{source}: expected a list or a dict with 'rules' key")

    rules: list[Rule] = []
    seen_ids: set[str] = set()
    for i, item in enumerate(raw_rules):
        if not isinstance(item, dict):
            raise RuleLoadError(f"{source}: rule #{i} is not a mapping")
        try:
            parsed = RuleYAML.model_validate(item)
        except ValueError as exc:
            raise RuleLoadError(f"{source}: rule #{i} ({item.get('id', '?')}): {exc}") from exc
        if parsed.id in seen_ids:
            raise RuleLoadError(f"{source}: duplicate rule id {parsed.id!r}")
        seen_ids.add(parsed.id)
        rules.append(parsed.to_rule())
    return rules


def load_rules(path: Path) -> list[Rule]:
    """从单个 YAML 文件加载规则集。

    文件可以是顶层 list，或 `{"rules": [...]}` dict。
    语法/重复 ID 错误在此抛出 RuleLoadError。
    """
    path = Path(path)
    if not path.is_file():
        raise RuleLoadError(f"rules file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RuleLoadError(f"{path}: YAML parse error: {exc}") from exc
    if raw is None:
        return []
    return _parse_rule_dicts(raw, str(path))


def load_rule_set(directory: Path) -> list[Rule]:
    """从目录加载所有 `.yaml`/`.yml` 规则文件，合并为一个规则列表。

    跨文件的重复 ID 也会被检测。文件按文件名排序加载，保证确定性。
    """
    directory = Path(directory)
    if not directory.is_dir():
        raise RuleLoadError(f"rules directory not found: {directory}")

    all_rules: list[Rule] = []
    seen_ids: set[str] = set()
    for f in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        file_rules = load_rules(f)
        for r in file_rules:
            if r.id in seen_ids:
                raise RuleLoadError(f"duplicate rule id {r.id!r} across files (in {f})")
            seen_ids.add(r.id)
            all_rules.append(r)
    # 确定性排序，保证审计可复现
    all_rules.sort(key=rule_sort_key)
    return all_rules


def compile_rule_set(rules: list[Rule]) -> HarnessEvaluator:
    """加载后编译规则集。CEL 语法错误在此批量暴露。

    生产用 compile_rule_with_functions（内置函数可用）。一条坏规则会让整个
    规则集加载失败 —— 不允许"部分规则加载"以免安全规则被静默跳过。
    """
    compiled = []
    for r in rules:
        try:
            compiled.append(compile_rule_with_functions(r))
        except EvaluationError as exc:
            raise RuleLoadError(f"rule {r.id}: {exc}") from exc
    logger.info("rule set compiled", extra={"rule_count": len(compiled)})
    return HarnessEvaluator(rules=compiled)


__all__ = [
    "RuleLoadError",
    "RuleYAML",
    "compile_rule_set",
    "load_rule_set",
    "load_rules",
]
