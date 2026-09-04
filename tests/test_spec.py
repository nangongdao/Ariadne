"""Spec 模块测试 —— 加载、派生、校验。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne.spec_module import (
    SpecLoadError,
    derive_evaluator,
    derive_goal,
    derive_rules,
    load_spec,
    load_spec_from_dict,
    validate_spec,
    validate_spec_or_raise,
)

# ---------- 最小可用 spec ----------


def _valid_spec_dict() -> dict:
    """返回一份能通过可验证性校验的最小 spec。"""
    return {
        "version": "1",
        "goal": {
            "task": "实现用户登录功能",
            "mode": "quality",
            "assertions": [
                {
                    "id": "tests-pass",
                    "kind": "command",
                    "spec": {"cmd": "pytest -x"},
                    "blocking": True,
                }
            ],
            "budget": {"max_iterations": 5, "max_total_tokens": 100000},
        },
        "rules": [],
        "sandbox": {"profile": "strict"},
    }


# ---------- 加载 ----------


class TestLoadSpec:
    """spec.yaml 加载。"""

    def test_load_from_dict_valid(self) -> None:
        spec = load_spec_from_dict(_valid_spec_dict())
        assert spec.version == "1"
        assert spec.goal.task == "实现用户登录功能"
        assert len(spec.goal.assertions) == 1

    def test_load_from_dict_missing_goal(self) -> None:
        with pytest.raises(SpecLoadError, match="schema validation failed"):
            load_spec_from_dict({"version": "1"})

    def test_load_from_dict_extra_field_rejected(self) -> None:
        """extra='forbid' 阻止未知字段。"""
        data = _valid_spec_dict()
        data["unknown_field"] = "x"
        with pytest.raises(SpecLoadError, match="schema validation failed"):
            load_spec_from_dict(data)

    def test_load_from_dict_empty_task(self) -> None:
        data = _valid_spec_dict()
        data["goal"]["task"] = ""
        with pytest.raises(SpecLoadError, match="schema validation failed"):
            load_spec_from_dict(data)

    def test_load_from_dict_empty_assertions(self) -> None:
        data = _valid_spec_dict()
        data["goal"]["assertions"] = []
        with pytest.raises(SpecLoadError, match="schema validation failed"):
            load_spec_from_dict(data)

    def test_load_from_file(self, tmp_path: Path) -> None:
        import yaml

        path = tmp_path / "spec.yaml"
        path.write_text(yaml.dump(_valid_spec_dict()), encoding="utf-8")
        spec = load_spec(path)
        assert spec.goal.task == "实现用户登录功能"

    def test_load_from_file_not_found(self) -> None:
        with pytest.raises(SpecLoadError, match="not found"):
            load_spec(Path("/nonexistent/spec.yaml"))

    def test_load_from_file_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.yaml"
        path.write_text("", encoding="utf-8")
        with pytest.raises(SpecLoadError, match="empty spec file"):
            load_spec(path)

    def test_load_from_file_bad_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.yaml"
        path.write_text("{invalid: yaml: [", encoding="utf-8")
        with pytest.raises(SpecLoadError, match="YAML parse error"):
            load_spec(path)


# ---------- 派生 Goal ----------


class TestDeriveGoal:
    """spec → Goal 派生。"""

    def test_derive_goal_basic(self) -> None:
        spec = load_spec_from_dict(_valid_spec_dict())
        goal = derive_goal(spec)
        assert goal.task == "实现用户登录功能"
        assert len(goal.assertions) == 1
        assert goal.assertions[0].id == "tests-pass"

    def test_derive_goal_budget(self) -> None:
        spec = load_spec_from_dict(_valid_spec_dict())
        goal = derive_goal(spec)
        assert goal.budget.max_iterations == 5
        assert goal.budget.max_total_tokens == 100000

    def test_derive_goal_mode(self) -> None:
        spec = load_spec_from_dict(_valid_spec_dict())
        goal = derive_goal(spec)
        assert goal.mode == "quality"

    def test_derive_goal_assertion_kind(self) -> None:
        spec = load_spec_from_dict(_valid_spec_dict())
        goal = derive_goal(spec)
        from ariadne.loop_module.goal import AssertionKind

        assert goal.assertions[0].kind == AssertionKind.COMMAND

    def test_derive_goal_unknown_kind_raises(self) -> None:
        data = _valid_spec_dict()
        data["goal"]["assertions"][0]["kind"] = "nonexistent"
        # Pydantic 接受任意字符串；枚举校验在 derive_goal 时 fail-fast
        spec = load_spec_from_dict(data)
        with pytest.raises(SpecLoadError, match="unknown assertion kind"):
            derive_goal(spec)

    def test_derive_goal_assertion_spec_preserved(self) -> None:
        spec = load_spec_from_dict(_valid_spec_dict())
        goal = derive_goal(spec)
        assert goal.assertions[0].spec == {"cmd": "pytest -x"}

    def test_derive_goal_blocking_flag(self) -> None:
        data = _valid_spec_dict()
        data["goal"]["assertions"].append(
            {
                "id": "style-check",
                "kind": "command",
                "spec": {"cmd": "ruff check ."},
                "blocking": False,
            }
        )
        spec = load_spec_from_dict(data)
        goal = derive_goal(spec)
        assert len(goal.blocking_assertions) == 1
        assert goal.blocking_assertions[0].id == "tests-pass"

    def test_derive_goal_default_budget(self) -> None:
        data = _valid_spec_dict()
        del data["goal"]["budget"]
        spec = load_spec_from_dict(data)
        goal = derive_goal(spec)
        assert goal.budget.max_iterations == 10  # BudgetSpec 默认值

    def test_derive_goal_stall_params(self) -> None:
        data = _valid_spec_dict()
        data["goal"]["stall_threshold"] = 5.0
        data["goal"]["stall_patience"] = 3
        spec = load_spec_from_dict(data)
        goal = derive_goal(spec)
        assert goal.stall_threshold == 5.0
        assert goal.stall_patience == 3


# ---------- 派生 Rules ----------


class TestDeriveRules:
    """spec → Rule 列表派生。"""

    def test_derive_rules_empty(self) -> None:
        spec = load_spec_from_dict(_valid_spec_dict())
        rules = derive_rules(spec)
        assert rules == []

    def test_derive_rules_basic(self) -> None:
        data = _valid_spec_dict()
        data["rules"] = [
            {
                "id": "block-pii",
                "category": "input",
                "hook": "pre_model",
                "when": "detect_pii(input.text).size() > 0",
                "action": "block",
                "severity": "critical",
                "message": "PII detected",
            }
        ]
        spec = load_spec_from_dict(data)
        rules = derive_rules(spec)
        assert len(rules) == 1
        assert rules[0].id == "block-pii"
        from ariadne.harness_module.models import Action, HookKind, RuleCategory, Severity

        assert rules[0].action == Action.BLOCK
        assert rules[0].hook == HookKind.PRE_MODEL
        assert rules[0].category == RuleCategory.INPUT
        assert rules[0].severity == Severity.CRITICAL

    def test_derive_rules_default_action_is_warn(self) -> None:
        """验收项 13：未指定 action 时默认 warn。"""
        data = _valid_spec_dict()
        data["rules"] = [
            {
                "id": "warn-length",
                "category": "input",
                "hook": "pre_model",
                "when": "input.text.size() > 10000",
            }
        ]
        spec = load_spec_from_dict(data)
        rules = derive_rules(spec)
        from ariadne.harness_module.models import Action

        assert rules[0].action == Action.WARN

    def test_derive_rules_unknown_category_raises(self) -> None:
        data = _valid_spec_dict()
        data["rules"] = [
            {
                "id": "bad",
                "category": "nonexistent",
                "hook": "pre_model",
                "when": "true",
            }
        ]
        spec = load_spec_from_dict(data)
        with pytest.raises(SpecLoadError, match="unknown category"):
            derive_rules(spec)

    def test_derive_rules_unknown_hook_raises(self) -> None:
        data = _valid_spec_dict()
        data["rules"] = [
            {
                "id": "bad",
                "category": "input",
                "hook": "nonexistent",
                "when": "true",
            }
        ]
        spec = load_spec_from_dict(data)
        with pytest.raises(SpecLoadError, match="unknown hook"):
            derive_rules(spec)

    def test_derive_rules_unknown_action_raises(self) -> None:
        data = _valid_spec_dict()
        data["rules"] = [
            {
                "id": "bad",
                "category": "input",
                "hook": "pre_model",
                "when": "true",
                "action": "nonexistent",
            }
        ]
        spec = load_spec_from_dict(data)
        with pytest.raises(SpecLoadError, match="unknown action"):
            derive_rules(spec)

    def test_derive_rules_unknown_severity_raises(self) -> None:
        data = _valid_spec_dict()
        data["rules"] = [
            {
                "id": "bad",
                "category": "input",
                "hook": "pre_model",
                "when": "true",
                "severity": "nonexistent",
            }
        ]
        spec = load_spec_from_dict(data)
        with pytest.raises(SpecLoadError, match="unknown severity"):
            derive_rules(spec)

    def test_derive_rules_rewrite_strategy(self) -> None:
        data = _valid_spec_dict()
        data["rules"] = [
            {
                "id": "rewrite-pii",
                "category": "input",
                "hook": "pre_model",
                "when": "detect_pii(input.text).size() > 0",
                "action": "rewrite",
                "rewrite_strategy": "redact_pii",
            }
        ]
        spec = load_spec_from_dict(data)
        rules = derive_rules(spec)
        assert rules[0].rewrite_strategy == "redact_pii"

    def test_derive_rules_route_target(self) -> None:
        data = _valid_spec_dict()
        data["rules"] = [
            {
                "id": "route-cheap",
                "category": "resource",
                "hook": "pre_model",
                "when": "ctx.loop.budget_used > 0.5",
                "action": "route",
                "route_target": "cheap-model",
            }
        ]
        spec = load_spec_from_dict(data)
        rules = derive_rules(spec)
        assert rules[0].route_target == "cheap-model"


# ---------- 派生 Evaluator ----------


class TestDeriveEvaluator:
    """spec → HarnessEvaluator 派生。"""

    def test_derive_evaluator_empty(self) -> None:
        spec = load_spec_from_dict(_valid_spec_dict())
        evaluator = derive_evaluator(spec)
        # 空规则集 → 全 ALLOW
        from ariadne.harness_module.models import Action, HarnessContext, HookKind

        ctx = HarnessContext(hook=HookKind.PRE_MODEL, input={"text": "anything"})
        decision = evaluator.evaluate(hook=HookKind.PRE_MODEL, context=ctx)
        assert decision.action == Action.ALLOW

    def test_derive_evaluator_with_rules(self) -> None:
        data = _valid_spec_dict()
        data["rules"] = [
            {
                "id": "block-pii",
                "category": "input",
                "hook": "pre_model",
                "when": "detect_pii(input.text).size() > 0",
                "action": "block",
                "severity": "critical",
            }
        ]
        spec = load_spec_from_dict(data)
        evaluator = derive_evaluator(spec)
        from ariadne.harness_module.models import Action, HarnessContext, HookKind

        # 无 PII → ALLOW
        ctx_safe = HarnessContext(hook=HookKind.PRE_MODEL, input={"text": "hello"})
        decision = evaluator.evaluate(hook=HookKind.PRE_MODEL, context=ctx_safe)
        assert decision.action == Action.ALLOW

        # 有 PII → BLOCK
        ctx_pii = HarnessContext(
            hook=HookKind.PRE_MODEL, input={"text": "my email is test@example.com"}
        )
        decision = evaluator.evaluate(hook=HookKind.PRE_MODEL, context=ctx_pii)
        assert decision.action == Action.BLOCK

    def test_derive_evaluator_syntax_error_raises(self) -> None:
        """CEL 语法错误在派生时 fail-fast。compile_rule_set 包装为 RuleLoadError。"""
        from ariadne.harness_module.loader import RuleLoadError

        data = _valid_spec_dict()
        data["rules"] = [
            {
                "id": "bad-syntax",
                "category": "input",
                "hook": "pre_model",
                "when": "input..text",  # 语法错误
                "action": "warn",
            }
        ]
        with pytest.raises(RuleLoadError):
            derive_evaluator(load_spec_from_dict(data))


# ---------- 校验 ----------


class TestValidateSpec:
    """spec 可验证性校验。"""

    def test_valid_spec_passes(self) -> None:
        spec = load_spec_from_dict(_valid_spec_dict())
        report = validate_spec(spec, sandbox_available=True)
        assert report.ok

    def test_validate_spec_or_raise_valid(self) -> None:
        spec = load_spec_from_dict(_valid_spec_dict())
        validate_spec_or_raise(spec, sandbox_available=True)  # 不抛

    def test_empty_assertions_rejected(self) -> None:
        """空断言在 Pydantic 层就被拦住（min_length=1）。"""
        data = _valid_spec_dict()
        data["goal"]["assertions"] = []
        with pytest.raises(SpecLoadError, match="schema validation failed"):
            load_spec_from_dict(data)

    def test_all_non_blocking_rejected(self) -> None:
        """全部 non-blocking 的目标不可验证。"""
        data = _valid_spec_dict()
        data["goal"]["assertions"][0]["blocking"] = False
        spec = load_spec_from_dict(data)
        report = validate_spec(spec, sandbox_available=True)
        assert not report.ok
        assert any("blocking" in i.message for i in report.errors)

    def test_empty_task_rejected(self) -> None:
        """空任务描述不可验证。"""
        data = _valid_spec_dict()
        data["goal"]["task"] = ""
        # Pydantic min_length=1 拦住
        with pytest.raises(SpecLoadError):
            load_spec_from_dict(data)

    def test_command_assertion_without_sandbox_rejected(self) -> None:
        """COMMAND 断言在沙箱不可用时被拒（error）。"""
        spec = load_spec_from_dict(_valid_spec_dict())
        report = validate_spec(spec, sandbox_available=False)
        # COMMAND 断言需要沙箱 —— 不可用时是 error
        assert not report.ok
        assert any(
            "沙箱" in i.message or "执行环境" in i.message
            for i in report.errors
        )

    def test_command_assertion_with_sandbox_ok(self) -> None:
        """COMMAND 断言在沙箱可用时通过。"""
        spec = load_spec_from_dict(_valid_spec_dict())
        report = validate_spec(spec, sandbox_available=True)
        assert report.ok

    def test_validate_spec_or_raise_invalid(self) -> None:
        """不可验证目标抛 GoalValidationError。"""
        from ariadne.loop_module.goal_validation import GoalValidationError

        data = _valid_spec_dict()
        data["goal"]["assertions"][0]["blocking"] = False
        spec = load_spec_from_dict(data)
        with pytest.raises(GoalValidationError):
            validate_spec_or_raise(spec, sandbox_available=True)

    def test_duplicate_assertion_ids_rejected(self) -> None:
        """重复断言 id 不可验证。"""
        data = _valid_spec_dict()
        data["goal"]["assertions"].append(
            {
                "id": "tests-pass",  # 重复
                "kind": "command",
                "spec": {"cmd": "ruff check ."},
                "blocking": True,
            }
        )
        spec = load_spec_from_dict(data)
        report = validate_spec(spec, sandbox_available=True)
        assert not report.ok
        assert any("重复" in i.message for i in report.errors)


# ---------- sandbox spec ----------


class TestSandboxSpec:
    """sandbox 段。"""

    def test_default_sandbox_strict(self) -> None:
        spec = load_spec_from_dict(_valid_spec_dict())
        assert spec.sandbox.profile.value == "strict"

    def test_custom_sandbox_profile(self) -> None:
        data = _valid_spec_dict()
        data["sandbox"] = {"profile": "trusted"}
        spec = load_spec_from_dict(data)
        assert spec.sandbox.profile.value == "trusted"

    def test_sandbox_allow_untrusted_default_false(self) -> None:
        spec = load_spec_from_dict(_valid_spec_dict())
        assert spec.sandbox.allow_untrusted_code is False

    def test_sandbox_custom_image(self) -> None:
        data = _valid_spec_dict()
        data["sandbox"] = {"image": "custom/runtime:3.12"}
        spec = load_spec_from_dict(data)
        assert spec.sandbox.image == "custom/runtime:3.12"


# ---------- version ----------


class TestSpecVersion:
    """version 字段。"""

    def test_default_version(self) -> None:
        spec = load_spec_from_dict(_valid_spec_dict())
        assert spec.version == "1"

    def test_custom_version(self) -> None:
        data = _valid_spec_dict()
        data["version"] = "2"
        spec = load_spec_from_dict(data)
        assert spec.version == "2"
