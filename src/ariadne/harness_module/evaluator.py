"""Harness 规则求值器 —— CEL 表达式求值 + fail-closed。

fail-closed 是安全底线（docs/04 第 1 节、M4-spec 第 9 节验收项 2）：
规则引擎自己挂了（CEL 语法错、求值超时、内部异常）不能变成放行一切，
而应视为拒绝。否则一个求值 bug 就会让所有硬约束失效。

求值是无状态纯函数（docs/04）：只看当前上下文，不看历史。
这保证求值结果可复现 —— 审计的前提。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import celpy

from ariadne.config import HarnessSettings
from ariadne.harness_module.functions import BUILTIN_FUNCTIONS
from ariadne.harness_module.models import (
    LOOP_CONTEXT_DEFAULTS,
    TOOL_CONTEXT_DEFAULTS,
    Action,
    Decision,
    HarnessContext,
    HookKind,
    Rule,
    RuleHit,
)
from ariadne.harness_module.resolve import resolve
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

# 求值超时上限。CEL 无循环，正常求值远低于此；超时说明输入异常或正则回溯失控。
DEFAULT_TIMEOUT_MS = 100


class EvaluationError(Exception):
    """规则求值失败。fail-closed 时调用方应转拒绝。"""


@dataclass
class CompiledRule:
    """预编译的规则。CEL 表达式编译一次复用，避免每轮重编译开销。

    编译时即发现 CEL 语法错误，而非运行时 —— 加载规则集时就拒绝坏规则。
    """

    rule: Rule
    program: celpy.Runner

    def evaluate(
        self,
        activation: Any,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        fail_closed: bool = True,
    ) -> Any:
        """求值。超时或异常时按 fail_closed 决定裁决方向。

        收 activation 而非 dict：同一次卡点求值里所有规则共享同一份上下文，
        json_to_cel 在调用方转一次即可（见 HarnessEvaluator.evaluate）。

        fail_closed=False 时求值故障按"未命中"处理（放行）。这个方向是
        **不安全**的，只为本地调试保留 —— 装配期会打 WARNING（见
        loop_worker._load_harness）。默认恒为 True。
        """
        start = time.perf_counter()
        try:
            result = self.program.evaluate(activation)
        except celpy.CELEvalError as exc:  # type: ignore[attr-defined]
            logger.warning(
                "cel eval error",
                extra={
                    "rule_id": self.rule.id,
                    "error": str(exc),
                    "fail_closed": fail_closed,
                },
            )
            return fail_closed  # fail-closed：求值失败视为命中（拒绝）
        except Exception as exc:
            logger.warning(
                "cel eval exception",
                extra={
                    "rule_id": self.rule.id,
                    "error": type(exc).__name__,
                    "fail_closed": fail_closed,
                },
            )
            return fail_closed
        elapsed_ms = (time.perf_counter() - start) * 1000
        if elapsed_ms > timeout_ms:
            logger.warning(
                "cel eval timeout",
                extra={
                    "rule_id": self.rule.id,
                    "elapsed_ms": round(elapsed_ms, 2),
                    "timeout_ms": timeout_ms,
                    "fail_closed": fail_closed,
                },
            )
            return fail_closed
        return result


@dataclass
class HarnessEvaluator:
    """规则集求值器。

    预编译所有规则（CEL 编译一次），按卡点过滤后批量求值，冲突消解出最终裁决。
    所有规则求值失败时整体 fail-closed（返回 block）。
    """

    rules: list[CompiledRule] = field(default_factory=list)
    # 求值策略。装配期从 settings.harness 注入（见 from_settings），使
    # ARIADNE_HARNESS_EVAL_TIMEOUT_MS / _FAIL_CLOSED 真正生效 —— 放在这一层
    # 而非 evaluate() 的入参，是因为生产调用方（engine._precheck、
    # GuardedLLMAdapter）不传超时，挂在入参上等于配置永远读不到。
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    fail_closed: bool = True

    @classmethod
    def from_settings(
        cls, rules: list[CompiledRule], settings: HarnessSettings
    ) -> HarnessEvaluator:
        """按 settings.harness 构造求值器，让超时与 fail-closed 旋钮生效。"""
        return cls(
            rules=rules,
            timeout_ms=settings.eval_timeout_ms,
            fail_closed=settings.fail_closed,
        )

    def evaluate(
        self,
        hook: HookKind,
        context: HarnessContext,
        *,
        timeout_ms: int | None = None,
    ) -> Decision:
        """对某卡点的所有适用规则求值，返回冲突消解后的裁决。

        timeout_ms 显式传入时覆盖实例策略（`ariadne rules test` 用来做压测）。
        """
        effective_timeout = self.timeout_ms if timeout_ms is None else timeout_ms
        applicable = [cr for cr in self.rules if cr.rule.hook is hook]
        if not applicable:
            return Decision(action=Action.ALLOW, hits=(), winning_hit=None)

        ctx_dict = self._context_to_dict(context)
        try:
            activation = celpy.json_to_cel(ctx_dict)  # type: ignore[attr-defined]
        except Exception as exc:
            # 上下文本身转不成 CEL 值：所有规则都无法求值，等价于全部命中。
            # 不能让异常穿出去 —— fail-closed 是安全底线，抛异常会被上层
            # 当成"求值没发生"而放行（docs/04 第 1 节）。
            logger.warning(
                "cel activation build failed",
                extra={
                    "error": type(exc).__name__,
                    "hook": hook.value,
                    "fail_closed": self.fail_closed,
                },
            )
            if not self.fail_closed:
                return Decision(action=Action.ALLOW, hits=(), winning_hit=None)
            return resolve(
                [
                    RuleHit(rule=cr.rule, value=True, message=cr.rule.message)
                    for cr in applicable
                ]
            )

        hits: list[RuleHit] = []
        for compiled in applicable:
            result = compiled.evaluate(
                activation,
                timeout_ms=effective_timeout,
                fail_closed=self.fail_closed,
            )
            if _is_truthy(result):
                hits.append(
                    RuleHit(
                        rule=compiled.rule,
                        value=result,
                        message=compiled.rule.message,
                    )
                )

        return resolve(hits)

    @staticmethod
    def _context_to_dict(context: HarnessContext) -> dict[str, Any]:
        """HarnessContext → CEL activation 可访问的 dict。

        规则侧通过 input.text / output.token_count / tool.name / ctx.loop 等访问。

        loop 走 _normalize_loop、tool 走 _normalize_tool 补全契约键。
        input/output 给空 dict 是兜不住的（`{}.text` 在 CEL 里照样是缺键
        错误），但它们的键由各卡点调用方按语义决定，无法预先声明；
        loop 和 tool 的键是固定契约（见 *_CONTEXT_DEFAULTS），可以也必须补全。
        """
        return {
            "input": context.input,
            "output": context.output,
            "tool": _normalize_tool(context.tool),
            "artifact": context.artifact,
            "usage": context.usage,
            "cost": context.cost,
            "ctx": {
                "loop": _normalize_loop(context.loop),
                "hook": context.hook.value,
            },
        }


def _normalize_loop(loop: dict[str, Any]) -> dict[str, Any]:
    """按 LOOP_CONTEXT_DEFAULTS 补全缺键并对齐数值类型。

    做两件事，两件都是为了让 resource 规则不再无条件命中：

    1. 补缺键。CEL 里缺键抛 KeyError 且污染 `&&` 两侧，会让
       `budget_limit > 0 && ...` 这种前置守卫失去作用 —— 没配上限时本该
       整条规则不触发，实际是 fail-closed 成命中。缺省 0 让守卫真正生效。
    2. 对齐类型。CEL 的 IntType / DoubleType 之间无隐式提升，
       `budget_limit * 0.9` 在 budget_limit 是 int 时直接无重载报错。
       契约声明为 float 的键统一转 float，声明为 int 的统一转 int。

    调用方多填的键原样保留（自定义规则可能读），只是不保证类型。
    """
    normalized = dict(loop)
    for key, default in LOOP_CONTEXT_DEFAULTS.items():
        value = normalized.get(key, default)
        try:
            normalized[key] = float(value) if isinstance(default, float) else int(value)
        except (TypeError, ValueError):
            # 调用方填了非数值（None / 字符串等）。退回缺省而非抛错：
            # 抛错会让整个 activation 构建失败，把一条规则的问题放大成全部命中。
            logger.warning(
                "loop context field not numeric, falling back to default",
                extra={"field": key, "value_type": type(value).__name__},
            )
            normalized[key] = default
    return normalized


def _normalize_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """按 TOOL_CONTEXT_DEFAULTS 补全缺键并转成字符串。

    pre_tool 的命中动作是 block，所以缺键的代价比 loop 那边更直接：
    `regex_match(tool.cmd, ...)` 在 cmd 缺失时求值错误 → fail-closed 命中
    → 命令被拦 → 断言记 errored → Loop 无法验证收敛。

    非 pre_tool 卡点传空 dict 时这里会补出 `cmd: ""`，无害 —— 那些卡点的
    规则不读 tool。
    """
    normalized = dict(tool)
    for key, default in TOOL_CONTEXT_DEFAULTS.items():
        value = normalized.get(key, default)
        normalized[key] = value if isinstance(value, str) else str(value)
    return normalized


def _is_truthy(value: Any) -> bool:
    """CEL 求值结果的真值判定。CEL 的 bool 直接用；其他类型按 Python 真值。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (list, dict, str)):
        return bool(value)
    return bool(value)


def _cel_annotations(*, with_functions: bool = False) -> dict[str, Any]:
    """CEL 环境的类型声明 + 可选函数声明。

    celpy 的 Environment 接受 annotations（不是 declarations）：
    类型声明让编译期检查字段存在性；函数声明让编译期校验函数调用签名。
    两者放在同一个 dict 里 —— celpy 把 callable 当函数、把 Type 当类型声明。
    """
    decls: dict[str, Any] = {
        "input": celpy.celtypes.MapType,
        "output": celpy.celtypes.MapType,
        "tool": celpy.celtypes.MapType,
        "artifact": celpy.celtypes.MapType,
        "usage": celpy.celtypes.MapType,
        "cost": celpy.celtypes.MapType,
        "ctx": celpy.celtypes.MapType,
    }
    if with_functions:
        for name, fn in BUILTIN_FUNCTIONS.items():
            decls[name] = fn
    return decls


def compile_rule(rule: Rule) -> CompiledRule:
    """编译单条规则（无内置函数）。CEL 语法错误在此暴露 —— 规则集加载时即拒绝坏规则。"""
    env = celpy.Environment(annotations=_cel_annotations())
    try:
        ast = env.compile(rule.when)
        program = env.program(ast)
    except celpy.CELParseError as exc:  # type: ignore[attr-defined]
        raise EvaluationError(f"规则 {rule.id} 的 CEL 表达式语法错误: {exc}") from exc
    return CompiledRule(rule=rule, program=program)


def compile_rules(rules: list[Rule]) -> HarnessEvaluator:
    """编译规则集（无内置函数）。语法错误在此批量暴露。"""
    compiled = [compile_rule(r) for r in rules]
    return HarnessEvaluator(rules=compiled)


def compile_rule_with_functions(rule: Rule) -> CompiledRule:
    """编译规则，内置函数在表达式中可用。

    两处必须同时注入函数：
    1. Environment(annotations=...) —— 编译期让 celpy 知道函数存在（避免"undeclared"）
    2. env.program(ast, functions=...) —— 运行时让求值器能 resolve_function
    缺任一会 KeyError 或 CELEvalError。
    """
    env = celpy.Environment(annotations=_cel_annotations(with_functions=True))
    try:
        ast = env.compile(rule.when)
        program = env.program(ast, functions=BUILTIN_FUNCTIONS)
    except celpy.CELParseError as exc:  # type: ignore[attr-defined]
        raise EvaluationError(f"规则 {rule.id} 的 CEL 表达式语法错误: {exc}") from exc
    return CompiledRule(rule=rule, program=program)


def compile_rules_with_functions(rules: list[Rule]) -> HarnessEvaluator:
    """编译规则集，内置函数可用。生产用这个（规则侧需要 detect_pii 等）。"""
    compiled = [compile_rule_with_functions(r) for r in rules]
    return HarnessEvaluator(rules=compiled)


__all__ = [
    "DEFAULT_TIMEOUT_MS",
    "CompiledRule",
    "EvaluationError",
    "HarnessEvaluator",
    "compile_rule",
    "compile_rule_with_functions",
    "compile_rules",
    "compile_rules_with_functions",
]
