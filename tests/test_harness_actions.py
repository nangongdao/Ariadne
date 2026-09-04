"""Harness actions 测试 —— 六动作分发。"""

from __future__ import annotations

import pytest

from ariadne.harness_module.actions import ActionResult, RewriteError, apply_decision
from ariadne.harness_module.models import (
    Action,
    Decision,
    HookKind,
    Rule,
    RuleCategory,
    RuleHit,
    Severity,
)


def make_rule(
    id: str = "test",
    action: Action = Action.WARN,
    severity: Severity = Severity.MEDIUM,
    rewrite_strategy: str = "",
    route_target: str = "",
) -> Rule:
    return Rule(
        id=id,
        category=RuleCategory.INPUT,
        hook=HookKind.PRE_MODEL,
        when="true",
        action=action,
        severity=severity,
        rewrite_strategy=rewrite_strategy,
        route_target=route_target,
    )


def make_decision(
    action: Action = Action.ALLOW,
    winning: RuleHit | None = None,
    hits: tuple[RuleHit, ...] = (),
    message: str = "",
    rewrite_strategy: str = "",
    route_target: str = "",
) -> Decision:
    return Decision(
        action=action,
        winning_hit=winning,
        hits=hits,
        message=message,
        rewrite_strategy=rewrite_strategy,
        route_target=route_target,
    )


class TestAllow:
    def test_allow_no_hits(self) -> None:
        d = make_decision(action=Action.ALLOW)
        r = apply_decision(d, "payload")
        assert r.action is Action.ALLOW
        assert not r.handled
        assert r.payload == "payload"
        assert r.should_continue

    def test_allow_with_hits_passthrough(self) -> None:
        hit = RuleHit(rule=make_rule())
        d = make_decision(action=Action.ALLOW, hits=(hit,))
        r = apply_decision(d, "payload")
        assert r.action is Action.ALLOW
        assert r.payload == "payload"
        assert r.should_continue


class TestWarn:
    def test_warn_passthrough_with_hit(self) -> None:
        rule = make_rule(id="warn-rule", action=Action.WARN)
        hit = RuleHit(rule=rule, message="warning!")
        d = make_decision(action=Action.WARN, winning=hit, hits=(hit,), message="warning!")
        r = apply_decision(d, "payload")
        assert r.action is Action.WARN
        assert not r.handled  # warn doesn't block
        assert r.payload == "payload"
        assert r.should_continue
        assert r.message == "warning!"


class TestBlock:
    def test_block_stops(self) -> None:
        rule = make_rule(id="block-rule", action=Action.BLOCK)
        hit = RuleHit(rule=rule)
        d = make_decision(action=Action.BLOCK, winning=hit, hits=(hit,), message="blocked!")
        r = apply_decision(d, "payload")
        assert r.action is Action.BLOCK
        assert r.blocked
        assert not r.should_continue
        assert r.message == "blocked!"
        assert r.winning_hit is hit

    def test_block_does_not_modify_payload(self) -> None:
        d = make_decision(action=Action.BLOCK)
        r = apply_decision(d, "original")
        assert r.payload is None  # block doesn't pass payload through


class TestRequireApproval:
    def test_approval_suspends(self) -> None:
        rule = make_rule(id="appr-rule", action=Action.REQUIRE_APPROVAL)
        hit = RuleHit(rule=rule)
        d = make_decision(action=Action.REQUIRE_APPROVAL, winning=hit, hits=(hit,))
        r = apply_decision(d, "payload")
        assert r.action is Action.REQUIRE_APPROVAL
        assert r.needs_approval
        assert not r.should_continue


class TestRoute:
    def test_route_returns_target(self) -> None:
        rule = make_rule(id="route-rule", action=Action.ROUTE, route_target="gpt-4o")
        hit = RuleHit(rule=rule)
        d = make_decision(
            action=Action.ROUTE, winning=hit, hits=(hit,), route_target="gpt-4o"
        )
        r = apply_decision(d, "payload")
        assert r.action is Action.ROUTE
        assert r.route_target == "gpt-4o"
        assert r.should_continue  # route continues, just to different target


class TestRewrite:
    def test_rewrite_applies_fn(self) -> None:
        rule = make_rule(id="rw-rule", action=Action.REWRITE, rewrite_strategy="redact")
        hit = RuleHit(rule=rule)
        d = make_decision(
            action=Action.REWRITE,
            winning=hit,
            hits=(hit,),
            rewrite_strategy="redact",
        )

        def rewrite_fn(strategy: str, payload: object) -> object:
            assert strategy == "redact"
            return "REDACTED"

        r = apply_decision(d, "original", rewrite_fn=rewrite_fn)
        assert r.action is Action.REWRITE
        assert r.payload == "REDACTED"
        assert r.rewrite_applied
        assert r.should_continue

    def test_rewrite_without_fn_raises(self) -> None:
        d = make_decision(action=Action.REWRITE, rewrite_strategy="redact")
        with pytest.raises(RewriteError, match="requires a rewrite_fn"):
            apply_decision(d, "payload")

    def test_rewrite_fn_error_propagates(self) -> None:
        d = make_decision(action=Action.REWRITE, rewrite_strategy="redact")

        def bad_fn(strategy: str, payload: object) -> object:
            raise RuntimeError("rewrite failed")

        with pytest.raises(RewriteError, match="rewrite failed"):
            apply_decision(d, "payload", rewrite_fn=bad_fn)


class TestActionResultProperties:
    def test_should_continue_true_for_allow(self) -> None:
        r = ActionResult(action=Action.ALLOW, payload="x")
        assert r.should_continue

    def test_should_continue_false_for_block(self) -> None:
        r = ActionResult(action=Action.BLOCK, blocked=True)
        assert not r.should_continue

    def test_should_continue_false_for_approval(self) -> None:
        r = ActionResult(action=Action.REQUIRE_APPROVAL, needs_approval=True)
        assert not r.should_continue

    def test_should_continue_true_for_route(self) -> None:
        r = ActionResult(action=Action.ROUTE, route_target="x")
        assert r.should_continue
