"""Harness loader 测试 —— YAML 加载 + schema 校验 + 编译。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne.harness_module.loader import (
    RuleLoadError,
    compile_rule_set,
    load_rule_set,
    load_rules,
)
from ariadne.harness_module.models import Action, HookKind, RuleCategory, Severity


@pytest.fixture
def rules_dir(tmp_path: Path) -> Path:
    """创建临时规则目录，含合法与非法规则文件。"""
    (tmp_path / "good.yaml").write_text(
        """
rules:
  - id: test-pii
    category: input
    hook: pre_model
    when: 'detect_pii(input.text).size() > 0'
    action: block
    severity: high
    message: "PII detected"
  - id: test-length
    category: input
    hook: pre_model
    when: 'token_count(input.text) > 100'
    action: warn
    severity: medium
""", encoding="utf-8")
    (tmp_path / "tool.yaml").write_text(
        """
rules:
  - id: cmd-wl
    category: tool
    hook: pre_tool
    when: '!regex_match(tool.cmd, "^(pytest|ruff)$")'
    action: block
    severity: critical
""", encoding="utf-8")
    return tmp_path


class TestLoadRules:
    """单文件加载。"""

    def test_load_valid_file(self, rules_dir: Path) -> None:
        rules = load_rules(rules_dir / "good.yaml")
        assert len(rules) == 2
        assert rules[0].id == "test-pii"
        assert rules[0].action is Action.BLOCK
        assert rules[0].severity is Severity.HIGH

    def test_load_top_level_list(self, tmp_path: Path) -> None:
        f = tmp_path / "list.yaml"
        f.write_text(
            """
- id: r1
  category: input
  hook: pre_model
  when: 'true'
- id: r2
  category: output
  hook: post_model
  when: 'true'
""", encoding="utf-8")
        rules = load_rules(f)
        assert len(rules) == 2

    def test_load_empty_file(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.yaml"
        f.write_text("", encoding="utf-8")
        assert load_rules(f) == []

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(RuleLoadError, match="not found"):
            load_rules(tmp_path / "nonexistent.yaml")

    def test_bad_category(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.yaml"
        f.write_text(
            """
rules:
  - id: bad
    category: nonsense
    hook: pre_model
    when: 'true'
""", encoding="utf-8")
        with pytest.raises(RuleLoadError):
            load_rules(f)

    def test_bad_hook(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.yaml"
        f.write_text(
            """
rules:
  - id: bad
    category: input
    hook: nonsense_hook
    when: 'true'
""", encoding="utf-8")
        with pytest.raises(RuleLoadError):
            load_rules(f)

    def test_duplicate_id(self, tmp_path: Path) -> None:
        f = tmp_path / "dup.yaml"
        f.write_text(
            """
rules:
  - id: dup
    category: input
    hook: pre_model
    when: 'true'
  - id: dup
    category: output
    hook: post_model
    when: 'true'
""", encoding="utf-8")
        with pytest.raises(RuleLoadError, match="duplicate"):
            load_rules(f)

    def test_id_with_whitespace(self, tmp_path: Path) -> None:
        f = tmp_path / "ws.yaml"
        f.write_text(
            """
rules:
  - id: "has space"
    category: input
    hook: pre_model
    when: 'true'
""", encoding="utf-8")
        with pytest.raises(RuleLoadError):
            load_rules(f)

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.yaml"
        f.write_text("{ this is not: valid: yaml", encoding="utf-8")
        with pytest.raises(RuleLoadError, match="YAML parse"):
            load_rules(f)

    def test_extra_field_rejected(self, tmp_path: Path) -> None:
        f = tmp_path / "extra.yaml"
        f.write_text(
            """
rules:
  - id: r1
    category: input
    hook: pre_model
    when: 'true'
    unknown_field: "oops"
""", encoding="utf-8")
        with pytest.raises(RuleLoadError):
            load_rules(f)


class TestDefaultActionWarn:
    """验收项 13：不指定 action 时默认 warn。"""

    def test_missing_action_defaults_warn(self, tmp_path: Path) -> None:
        f = tmp_path / "default.yaml"
        f.write_text(
            """
rules:
  - id: r1
    category: input
    hook: pre_model
    when: 'true'
""", encoding="utf-8")
        rules = load_rules(f)
        assert rules[0].action is Action.WARN

    def test_explicit_action_preserved(self, tmp_path: Path) -> None:
        f = tmp_path / "explicit.yaml"
        f.write_text(
            """
rules:
  - id: r1
    category: input
    hook: pre_model
    when: 'true'
    action: block
""", encoding="utf-8")
        rules = load_rules(f)
        assert rules[0].action is Action.BLOCK


class TestLoadRuleSet:
    """目录批量加载。"""

    def test_load_directory(self, rules_dir: Path) -> None:
        rules = load_rule_set(rules_dir)
        assert len(rules) == 3
        ids = {r.id for r in rules}
        assert ids == {"test-pii", "test-length", "cmd-wl"}

    def test_cross_file_duplicate_id(self, tmp_path: Path) -> None:
        (tmp_path / "a.yaml").write_text(
            """
rules:
  - id: dup
    category: input
    hook: pre_model
    when: 'true'
""", encoding="utf-8")
        (tmp_path / "b.yaml").write_text(
            """
rules:
  - id: dup
    category: output
    hook: post_model
    when: 'true'
""", encoding="utf-8")
        with pytest.raises(RuleLoadError, match="duplicate"):
            load_rule_set(tmp_path)

    def test_directory_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(RuleLoadError, match="not found"):
            load_rule_set(tmp_path / "nonexistent")

    def test_deterministic_order(self, rules_dir: Path) -> None:
        """同目录多次加载顺序一致。"""
        rules1 = load_rule_set(rules_dir)
        rules2 = load_rule_set(rules_dir)
        assert [r.id for r in rules1] == [r.id for r in rules2]


class TestCompileRuleSet:
    def test_compile_valid_rules(self, rules_dir: Path) -> None:
        rules = load_rule_set(rules_dir)
        evaluator = compile_rule_set(rules)
        assert len(evaluator.rules) == 3

    def test_compile_bad_cel(self, tmp_path: Path) -> None:
        from ariadne.harness_module.loader import compile_rule_set
        from ariadne.harness_module.models import Rule

        bad_rule = Rule(
            id="bad",
            category=RuleCategory.INPUT,
            hook=HookKind.PRE_MODEL,
            when="this is not valid cel at all",
        )
        with pytest.raises(RuleLoadError):
            compile_rule_set([bad_rule])


class TestBuiltInRules:
    """内置规则文件加载验证。"""

    def test_builtin_rules_load(self) -> None:
        rules_path = Path(__file__).parent.parent / "src" / "ariadne" / "harness_module" / "rules"
        rules = load_rule_set(rules_path)
        assert len(rules) >= 15  # 16 built-in rules
        evaluator = compile_rule_set(rules)
        assert len(evaluator.rules) == len(rules)

    def test_all_categories_covered(self) -> None:
        rules_path = Path(__file__).parent.parent / "src" / "ariadne" / "harness_module" / "rules"
        rules = load_rule_set(rules_path)
        categories = {r.category for r in rules}
        assert RuleCategory.INPUT in categories
        assert RuleCategory.OUTPUT in categories
        assert RuleCategory.RESOURCE in categories
        assert RuleCategory.TOOL in categories
