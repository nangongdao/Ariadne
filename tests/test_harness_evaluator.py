"""HarnessEvaluator 测试 —— CEL 求值 + fail-closed + 内置函数。"""

from __future__ import annotations

import pytest

from ariadne.harness_module.evaluator import (
    EvaluationError,
    HarnessEvaluator,
    compile_rule,
    compile_rule_with_functions,
)
from ariadne.harness_module.models import (
    Action,
    HarnessContext,
    HookKind,
    Rule,
    RuleCategory,
    Severity,
)


def make_rule(
    id: str = "test",
    hook: HookKind = HookKind.PRE_MODEL,
    when: str = "true",
    action: Action = Action.WARN,
    category: RuleCategory = RuleCategory.INPUT,
    severity: Severity = Severity.MEDIUM,
) -> Rule:
    return Rule(id=id, category=category, hook=hook, when=when, action=action, severity=severity)


class TestBasicEvaluation:
    """基础求值：命中/不命中、空规则集。"""

    def test_empty_ruleset_returns_allow(self) -> None:
        ev = HarnessEvaluator(rules=[])
        ctx = HarnessContext(hook=HookKind.PRE_MODEL)
        d = ev.evaluate(HookKind.PRE_MODEL, ctx)
        assert d.action is Action.ALLOW
        assert not d.hits

    def test_rule_not_matching_returns_allow(self) -> None:
        rule = make_rule(when="false")
        ev = HarnessEvaluator(rules=[compile_rule(rule)])
        ctx = HarnessContext(hook=HookKind.PRE_MODEL)
        d = ev.evaluate(HookKind.PRE_MODEL, ctx)
        assert d.action is Action.ALLOW

    def test_rule_matching_returns_action(self) -> None:
        rule = make_rule(when="true", action=Action.BLOCK)
        ev = HarnessEvaluator(rules=[compile_rule(rule)])
        ctx = HarnessContext(hook=HookKind.PRE_MODEL)
        d = ev.evaluate(HookKind.PRE_MODEL, ctx)
        assert d.action is Action.BLOCK
        assert d.blocked

    def test_hook_filtering(self) -> None:
        """只求值匹配 hook 的规则。"""
        rule_pre = make_rule(id="pre", hook=HookKind.PRE_MODEL, when="true", action=Action.BLOCK)
        rule_post = make_rule(id="post", hook=HookKind.POST_MODEL, when="true", action=Action.WARN)
        ev = HarnessEvaluator(rules=[compile_rule(rule_pre), compile_rule(rule_post)])
        ctx = HarnessContext(hook=HookKind.PRE_MODEL)
        d = ev.evaluate(HookKind.PRE_MODEL, ctx)
        assert d.action is Action.BLOCK
        assert len(d.hits) == 1
        assert d.hits[0].rule.id == "pre"

    def test_context_access_input_text(self) -> None:
        rule = make_rule(when='input.text == "hello"', action=Action.BLOCK)
        ev = HarnessEvaluator(rules=[compile_rule(rule)])
        ctx = HarnessContext(hook=HookKind.PRE_MODEL, input={"text": "hello"})
        d = ev.evaluate(HookKind.PRE_MODEL, ctx)
        assert d.blocked

        ctx2 = HarnessContext(hook=HookKind.PRE_MODEL, input={"text": "world"})
        d2 = ev.evaluate(HookKind.PRE_MODEL, ctx2)
        assert not d2.blocked

    def test_context_access_ctx_loop(self) -> None:
        rule = make_rule(when="ctx.loop.iteration > 5", action=Action.BLOCK)
        ev = HarnessEvaluator(rules=[compile_rule(rule)])
        ctx = HarnessContext(hook=HookKind.PRE_MODEL, loop={"iteration": 10})
        d = ev.evaluate(HookKind.PRE_MODEL, ctx)
        assert d.blocked


class TestFailClosed:
    """fail-closed：求值失败时视为命中（拒绝）。"""

    def test_cel_syntax_error_raises_on_compile(self) -> None:
        rule = make_rule(when="this is not valid cel")
        with pytest.raises(EvaluationError):
            compile_rule(rule)

    def test_missing_field_returns_true_fail_closed(self) -> None:
        """访问不存在的字段时 CEL 报错 → fail-closed 返回 True（命中）。"""
        rule = make_rule(when="input.nonexistent_field == 42", action=Action.BLOCK)
        ev = HarnessEvaluator(rules=[compile_rule(rule)])
        ctx = HarnessContext(hook=HookKind.PRE_MODEL, input={"text": "hello"})
        d = ev.evaluate(HookKind.PRE_MODEL, ctx)
        # fail-closed: 命中 → block
        assert d.action is Action.BLOCK


class TestBuiltinFunctions:
    """内置函数在 CEL 表达式中可调用。"""

    def test_detect_pii_hit(self) -> None:
        rule = make_rule(
            when="detect_pii(input.text).size() > 0", action=Action.BLOCK
        )
        ev = HarnessEvaluator(rules=[compile_rule_with_functions(rule)])
        ctx = HarnessContext(hook=HookKind.PRE_MODEL, input={"text": "email me at a@b.com"})
        d = ev.evaluate(HookKind.PRE_MODEL, ctx)
        assert d.blocked

    def test_detect_pii_no_hit(self) -> None:
        rule = make_rule(
            when="detect_pii(input.text).size() > 0", action=Action.BLOCK
        )
        ev = HarnessEvaluator(rules=[compile_rule_with_functions(rule)])
        ctx = HarnessContext(hook=HookKind.PRE_MODEL, input={"text": "no sensitive data"})
        d = ev.evaluate(HookKind.PRE_MODEL, ctx)
        assert not d.blocked

    def test_regex_match_hit(self) -> None:
        rule = make_rule(
            when='regex_match(input.text, "^(pytest|ruff)$")', action=Action.BLOCK
        )
        ev = HarnessEvaluator(rules=[compile_rule_with_functions(rule)])
        ctx = HarnessContext(hook=HookKind.PRE_MODEL, input={"text": "pytest"})
        d = ev.evaluate(HookKind.PRE_MODEL, ctx)
        assert d.blocked

    def test_regex_match_no_hit(self) -> None:
        rule = make_rule(
            when='regex_match(input.text, "^(pytest|ruff)$")', action=Action.BLOCK
        )
        ev = HarnessEvaluator(rules=[compile_rule_with_functions(rule)])
        ctx = HarnessContext(hook=HookKind.PRE_MODEL, input={"text": "rm -rf /"})
        d = ev.evaluate(HookKind.PRE_MODEL, ctx)
        assert not d.blocked

    def test_count_citations(self) -> None:
        rule = make_rule(
            hook=HookKind.POST_MODEL,
            when="count_citations(output.text) < 1",
            action=Action.WARN,
        )
        ev = HarnessEvaluator(rules=[compile_rule_with_functions(rule)])
        ctx = HarnessContext(
            hook=HookKind.POST_MODEL, output={"text": "see [1] and [2]"}
        )
        d = ev.evaluate(HookKind.POST_MODEL, ctx)
        assert d.action is Action.ALLOW  # 2 citations >= 1, no hit

        ctx2 = HarnessContext(hook=HookKind.POST_MODEL, output={"text": "no refs"})
        d2 = ev.evaluate(HookKind.POST_MODEL, ctx2)
        assert d2.action is Action.WARN

    def test_json_valid(self) -> None:
        rule = make_rule(
            hook=HookKind.POST_MODEL,
            when='json_valid(output.text)',
            action=Action.ALLOW,
        )
        ev = HarnessEvaluator(rules=[compile_rule_with_functions(rule)])
        ctx = HarnessContext(hook=HookKind.POST_MODEL, output={"text": '{"key": 1}'})
        d = ev.evaluate(HookKind.POST_MODEL, ctx)
        assert d.action is Action.ALLOW

    def test_token_count(self) -> None:
        rule = make_rule(when="token_count(input.text) > 10", action=Action.WARN)
        ev = HarnessEvaluator(rules=[compile_rule_with_functions(rule)])
        ctx = HarnessContext(hook=HookKind.PRE_MODEL, input={"text": "short"})
        d = ev.evaluate(HookKind.PRE_MODEL, ctx)
        assert d.action is Action.ALLOW  # "short" is ~1-2 tokens, < 10


class TestMultipleRules:
    """多规则求值 + 冲突消解。"""

    def test_multiple_hits_resolved(self) -> None:
        """多条命中时按优先级消解。"""
        r_warn = make_rule(id="warn-rule", when="true", action=Action.WARN)
        r_block = make_rule(id="block-rule", when="true", action=Action.BLOCK)
        ev = HarnessEvaluator(rules=[compile_rule(r_warn), compile_rule(r_block)])
        ctx = HarnessContext(hook=HookKind.PRE_MODEL)
        d = ev.evaluate(HookKind.PRE_MODEL, ctx)
        assert d.action is Action.BLOCK  # block > warn
        assert len(d.hits) == 2

    def test_all_hits_recorded(self) -> None:
        """全部命中都记入 hits（不止胜出者）。"""
        r1 = make_rule(id="r1", when="true", action=Action.WARN)
        r2 = make_rule(id="r2", when="true", action=Action.WARN)
        r3 = make_rule(id="r3", when="true", action=Action.BLOCK)
        ev = HarnessEvaluator(rules=[compile_rule(r1), compile_rule(r2), compile_rule(r3)])
        d = ev.evaluate(HookKind.PRE_MODEL, HarnessContext(hook=HookKind.PRE_MODEL))
        assert len(d.hits) == 3
        assert d.winning_hit is not None
        assert d.winning_hit.rule.id == "r3"  # block wins
