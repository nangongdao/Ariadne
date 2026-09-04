"""冲突消解测试 —— 确定性排序 + 优先级。"""

from __future__ import annotations

from ariadne.harness_module.models import (
    ACTION_PRIORITY,
    Action,
    Rule,
    RuleCategory,
    RuleHit,
    Severity,
)
from ariadne.harness_module.resolve import resolve, rule_sort_key


def make_rule(
    id: str = "test",
    action: Action = Action.WARN,
    severity: Severity = Severity.MEDIUM,
    category: RuleCategory = RuleCategory.INPUT,
    rewrite_strategy: str = "",
    route_target: str = "",
) -> Rule:
    return Rule(
        id=id,
        category=category,
        hook=__import__("ariadne.harness_module.models", fromlist=["HookKind"]).HookKind.PRE_MODEL,
        when="true",
        action=action,
        severity=severity,
        rewrite_strategy=rewrite_strategy,
        route_target=route_target,
    )


def make_hit(rule: Rule) -> RuleHit:
    return RuleHit(rule=rule)


class TestResolveBasic:
    """resolve 的基本行为。"""

    def test_empty_hits_returns_allow(self) -> None:
        d = resolve([])
        assert d.action is Action.ALLOW
        assert d.hits == ()
        assert d.winning_hit is None

    def test_single_hit_returns_its_action(self) -> None:
        rule = make_rule(action=Action.BLOCK)
        d = resolve([make_hit(rule)])
        assert d.action is Action.BLOCK
        assert d.blocked

    def test_all_hits_recorded(self) -> None:
        r1 = make_rule(id="a", action=Action.WARN)
        r2 = make_rule(id="b", action=Action.BLOCK)
        d = resolve([make_hit(r1), make_hit(r2)])
        assert len(d.hits) == 2


class TestPriorityOrder:
    """ACTION_PRIORITY 的顺序：block > require_approval > route > rewrite > warn > allow。"""

    def test_full_priority_order(self) -> None:
        """每种 action 的规则都命中时，block 胜出。"""
        rules = [
            make_rule(id="allow", action=Action.ALLOW),
            make_rule(id="warn", action=Action.WARN),
            make_rule(id="rewrite", action=Action.REWRITE),
            make_rule(id="route", action=Action.ROUTE),
            make_rule(id="approval", action=Action.REQUIRE_APPROVAL),
            make_rule(id="block", action=Action.BLOCK),
        ]
        hits = [make_hit(r) for r in rules]
        d = resolve(hits)
        assert d.action is Action.BLOCK
        assert d.winning_hit is not None
        assert d.winning_hit.rule.id == "block"

    def test_block_beats_approval(self) -> None:
        r_block = make_rule(id="b", action=Action.BLOCK)
        r_approval = make_rule(id="a", action=Action.REQUIRE_APPROVAL)
        d = resolve([make_hit(r_approval), make_hit(r_block)])
        assert d.action is Action.BLOCK

    def test_approval_beats_route(self) -> None:
        r_route = make_rule(id="r", action=Action.ROUTE)
        r_approval = make_rule(id="a", action=Action.REQUIRE_APPROVAL)
        d = resolve([make_hit(r_route), make_hit(r_approval)])
        assert d.action is Action.REQUIRE_APPROVAL
        assert d.needs_approval

    def test_route_beats_rewrite(self) -> None:
        r_route = make_rule(id="r", action=Action.ROUTE, route_target="gpt-4o")
        r_rewrite = make_rule(id="w", action=Action.REWRITE)
        d = resolve([make_hit(r_rewrite), make_hit(r_route)])
        assert d.action is Action.ROUTE
        assert d.route_target == "gpt-4o"

    def test_rewrite_beats_warn(self) -> None:
        r_rewrite = make_rule(id="w", action=Action.REWRITE, rewrite_strategy="redact")
        r_warn = make_rule(id="n", action=Action.WARN)
        d = resolve([make_hit(r_warn), make_hit(r_rewrite)])
        assert d.action is Action.REWRITE
        assert d.rewrite_strategy == "redact"

    def test_warn_beats_allow(self) -> None:
        r_warn = make_rule(id="w", action=Action.WARN)
        r_allow = make_rule(id="a", action=Action.ALLOW)
        d = resolve([make_hit(r_allow), make_hit(r_warn)])
        assert d.action is Action.WARN

    def test_priority_matches_constant(self) -> None:
        assert ACTION_PRIORITY == (
            Action.BLOCK,
            Action.REQUIRE_APPROVAL,
            Action.ROUTE,
            Action.REWRITE,
            Action.WARN,
            Action.ALLOW,
        )


class TestTieBreaking:
    """同优先级时按 severity → rule_id 排序。"""

    def test_higher_severity_wins(self) -> None:
        r_low = make_rule(id="low", action=Action.WARN, severity=Severity.LOW)
        r_high = make_rule(id="high", action=Action.WARN, severity=Severity.HIGH)
        d = resolve([make_hit(r_low), make_hit(r_high)])
        assert d.winning_hit is not None
        assert d.winning_hit.rule.severity is Severity.HIGH

    def test_same_severity_rule_id_order(self) -> None:
        r_b = make_rule(id="bbb", action=Action.WARN, severity=Severity.MEDIUM)
        r_a = make_rule(id="aaa", action=Action.WARN, severity=Severity.MEDIUM)
        d = resolve([make_hit(r_b), make_hit(r_a)])
        assert d.winning_hit is not None
        assert d.winning_hit.rule.id == "aaa"  # lexicographic

    def test_critical_severity_wins_over_all_same_action(self) -> None:
        rules = [
            make_rule(id=f"r{i}", action=Action.BLOCK, severity=s)
            for i, s in enumerate(
                [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
            )
        ]
        d = resolve([make_hit(r) for r in rules])
        assert d.winning_hit is not None
        assert d.winning_hit.rule.severity is Severity.CRITICAL


class TestDeterminism:
    """确定性：同输入多次 resolve 结果一致。"""

    def test_same_input_same_output(self) -> None:
        rules = [
            make_rule(id="a", action=Action.WARN, severity=Severity.HIGH),
            make_rule(id="b", action=Action.WARN, severity=Severity.HIGH),
            make_rule(id="c", action=Action.BLOCK, severity=Severity.LOW),
        ]
        hits = [make_hit(r) for r in rules]
        d1 = resolve(hits)
        d2 = resolve(hits)
        assert d1.action == d2.action
        assert d1.winning_hit == d2.winning_hit
        assert [h.rule.id for h in d1.hits] == [h.rule.id for h in d2.hits]

    def test_order_independence(self) -> None:
        """hits 传入顺序不影响结果。"""
        r_warn = make_rule(id="w", action=Action.WARN, severity=Severity.HIGH)
        r_block = make_rule(id="b", action=Action.BLOCK, severity=Severity.LOW)
        d1 = resolve([make_hit(r_warn), make_hit(r_block)])
        d2 = resolve([make_hit(r_block), make_hit(r_warn)])
        assert d1.action == d2.action
        assert d1.winning_hit == d2.winning_hit


class TestRuleSortKey:
    def test_sort_key_ordering(self) -> None:
        r_block = make_rule(id="b", action=Action.BLOCK)
        r_warn = make_rule(id="a", action=Action.WARN)
        rules = [r_warn, r_block]
        rules.sort(key=rule_sort_key)
        assert rules[0].action is Action.BLOCK  # block sorts first
