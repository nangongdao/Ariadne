"""Harness 规则求值基准 —— 验收项 1（p99 < 5ms）的实测结论。

**验收项 1 未达标，且原先记录的"通过"是量错了对象。**

原基准测的是文件内自造的 3 条规则（`pre_model` 只 2 条），据此记下
p99 = 1.03ms 并在 docs/M4-spec.md 标了通过。但线上随包规则集
（`harness_module/rules/*.yaml`）`pre_model` 有 5 条、`pre_tool` 有 6 条，
实测 p99 在 13~25ms。所以这里改成量**随包规则集**：验收项说的是规则求值，
不是某个便于达标的子集。

为什么不达标是结构性的、而非调优不足：celpy 是解释型求值器，每次
`evaluate()` 重走一遍 lark 解析树。实测单条规则约 2.6ms，且耗时与表达式
AST 规模相关而与内置函数的实际工作量无关 —— `resource-iteration-warn`
是纯整数比较（无函数调用）也要 2.1ms。拆解：`_context_to_dict` 0.1%、
`json_to_cel` 3~6%、纯 CEL 求值 >100%（其余是循环与 resolve 开销）。
把 5 条规则压进 5ms 需要单条 < 1ms，celpy 做不到。

celpy 的 `CompiledRunner`（把 CEL 转写成 Python 再 eval）能快一个量级，
**明确否决**：规则表达式可经规则 API 由租户提供，`eval` 等于把任意代码
执行权交出去（docs/04 第 2.2 节已就此否决 eval/exec）。宁可慢，不可不安全。

延迟的实际影响：单次卡点 10~25ms，相对 LLM 调用的 1000~5000ms 是
0.2%~2.5%。M6 的"平台额外延迟 < 50ms P95"仍然满足 —— 真正的问题是
docs 记了个不成立的数字，不是这点延迟。

## 断言用比值而非墙钟

本机墙钟不可比：同一段测量分钟级内测出过 p50=3.4ms 与 13.7ms（4 倍差）。
所以断言的是「被测 / 同进程纯 Python 基线」的比值 —— 机器忙时两者同等
变慢，比值稳定。绝对毫秒只记录不断言，避免把环境抖动变成红灯。
"""

from __future__ import annotations

import time
from pathlib import Path
from statistics import median, quantiles

import pytest

from ariadne.harness_module.evaluator import HarnessEvaluator, compile_rule_with_functions
from ariadne.harness_module.loader import compile_rule_set, load_rule_set
from ariadne.harness_module.models import (
    Action,
    HarnessContext,
    HookKind,
    Rule,
    RuleCategory,
    Severity,
)
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

RULES_DIR = Path(__file__).resolve().parents[1] / "src" / "ariadne" / "harness_module" / "rules"

# 输入文本含 PII，触发 detect_pii 规则
_PII_INPUT = "联系我：email: alice@example.com, phone: 13800138000"
_CLEAN_INPUT = "这是一段普通的文本，没有敏感信息"

# 基线工作量。规模无所谓，只要够大到盖过计时噪声、且不含 I/O。
_BASELINE_OPS = 2000

# 比值上限：单条规则 ≈ 4x 基线，随包 pre_model 5 条留足余量。
# 卡的是「量级没变」而不是精确值 —— 换 CEL 实现或规则数翻倍才该触发。
_MAX_RATIO_PER_RULE = 12.0


def _baseline_work() -> int:
    """纯 Python 定量工作，用作负载归一化的分母。无 I/O、无分配大对象。"""
    total = 0
    for i in range(_BASELINE_OPS):
        total += i * 3 % 7
    return total


def _build_evaluator() -> HarnessEvaluator:
    """随包规则集（线上真正加载的那一份）。"""
    rules = load_rule_set(RULES_DIR)
    return HarnessEvaluator(rules=[compile_rule_with_functions(r) for r in rules])


def _build_synthetic_evaluator() -> HarnessEvaluator:
    """3 条自造规则，用于确定性测试（不用于延迟结论）。"""
    rules = [
        Rule(
            id="block-pii",
            category=RuleCategory.INPUT,
            hook=HookKind.PRE_MODEL,
            when="detect_pii(input.text).size() > 0",
            action=Action.BLOCK,
            severity=Severity.CRITICAL,
            message="PII detected",
        ),
        Rule(
            id="warn-no-citation",
            category=RuleCategory.OUTPUT,
            hook=HookKind.POST_MODEL,
            when="count_citations(output.text) < 1",
            action=Action.WARN,
            severity=Severity.LOW,
        ),
        Rule(
            id="block-budget",
            category=RuleCategory.RESOURCE,
            hook=HookKind.PRE_MODEL,
            # 三处都不能随手写，写错了测到的就是异常路径而非求值路径：
            # 1. `ctx.loop` 而非 `loop` —— 上下文契约见 models.LOOP_CONTEXT_DEFAULTS
            # 2. 与整型字面量 `100` 比而非 `100.0` —— budget_used 契约是 int，
            #    CEL 的 IntType/DoubleType 无隐式提升，int > double 直接无重载报错
            # 3. 只要报错就 fail-closed 成命中，测出来又快又"正常"，但量的是 except 分支
            when="ctx.loop.budget_used > 100",
            action=Action.BLOCK,
            severity=Severity.HIGH,
        ),
    ]
    return compile_rule_set(rules)


def _percentiles(latencies_ms: list[float]) -> dict[str, float]:
    """从延迟列表计算 p50/p99/p999。"""
    if len(latencies_ms) < 100:
        return {"p50": 0.0, "p99": 0.0, "p999": 0.0}
    qs = quantiles(latencies_ms, n=1000, method="inclusive")
    return {"p50": qs[499], "p99": qs[989], "p999": qs[998]}


def _measure(fn: object, n: int) -> list[float]:
    """跑 n 次，返回每次耗时（毫秒）。"""
    out: list[float] = []
    for _ in range(n):
        start = time.perf_counter()
        fn()  # type: ignore[operator]
        out.append((time.perf_counter() - start) * 1000)
    return out


@pytest.mark.slow
class TestHarnessBenchmark:
    """验收项 1 的实测：随包规则集的求值延迟。"""

    def test_eval_latency_normalized_ratio(self) -> None:
        """随包 pre_model 规则集的求值成本，以纯 Python 基线归一化。

        断言比值而非墙钟：本机墙钟同段测量差过 4 倍，断墙钟等于随机红灯。
        """
        evaluator = _build_evaluator()
        n_rules = len([cr for cr in evaluator.rules if cr.rule.hook is HookKind.PRE_MODEL])
        assert n_rules > 0, "随包规则集没有 pre_model 规则，基准失去意义"

        ctx_pii = HarnessContext(
            hook=HookKind.PRE_MODEL,
            input={"text": _PII_INPUT},
            loop={"budget_used": 50, "budget_limit": 200_000},
        )
        ctx_clean = HarnessContext(
            hook=HookKind.PRE_MODEL,
            input={"text": _CLEAN_INPUT},
            loop={"budget_used": 50, "budget_limit": 200_000},
        )

        # 预热：把惰性 import / 正则编译移出测量窗口。
        # 不预热会把首次的一次性开销算进 p999，且掩盖真实稳态成本。
        for _ in range(20):
            _baseline_work()
            evaluator.evaluate(hook=HookKind.PRE_MODEL, context=ctx_pii)

        latencies: list[float] = []
        baselines: list[float] = []
        # 交替 PII / clean 输入，避免只测到一条分支
        for i in range(1000):
            ctx = ctx_pii if i % 2 == 0 else ctx_clean
            latencies.extend(
                _measure(lambda c=ctx: evaluator.evaluate(hook=HookKind.PRE_MODEL, context=c), 1)
            )
            if i % 10 == 0:  # 基线交替采样，跟随同期机器负载
                baselines.extend(_measure(_baseline_work, 1))

        pcts = _percentiles(latencies)
        baseline_med = median(baselines)
        ratio = pcts["p50"] / baseline_med
        ratio_per_rule = ratio / n_rules

        logger.info(
            "harness eval latency (shipped rules)",
            extra={
                "hook": HookKind.PRE_MODEL.value,
                "rules": n_rules,
                "p50_ms": round(pcts["p50"], 3),
                "p99_ms": round(pcts["p99"], 3),
                "p999_ms": round(pcts["p999"], 3),
                "baseline_ms": round(baseline_med, 4),
                "ratio_vs_baseline": round(ratio, 2),
                "ratio_per_rule": round(ratio_per_rule, 2),
                "target_5ms_met": pcts["p99"] < 5.0,
            },
        )

        assert ratio_per_rule < _MAX_RATIO_PER_RULE, (
            f"单条规则成本 {ratio_per_rule:.2f}x 基线，超过 {_MAX_RATIO_PER_RULE}x。"
            f"（p50={pcts['p50']:.3f}ms / {n_rules} 条 / 基线={baseline_med:.4f}ms）"
            "量级变化说明求值路径退化，或规则集规模变了 —— 两者都该看一眼。"
        )

    def test_cold_start_within_timeout(self) -> None:
        """首次求值必须落在 DEFAULT_TIMEOUT_MS 内。

        这条是回归防线，不是性能指标。曾经 `estimate_tokens` 在函数体内惰性
        import loop_module（约 3.3s 冷导入），使首次 evaluate() 耗时 757ms >
        超时上限 100ms，被 fail-closed 判成命中 —— worker 起来后第一个请求
        无故被拦，日志只显示 "cel eval timeout"，查不到真凶是 import。

        所以断言的是**全新 evaluator 的第一次**求值：预热会掩盖这类缺陷。
        """
        from ariadne.harness_module.evaluator import DEFAULT_TIMEOUT_MS

        evaluator = _build_evaluator()  # 全新实例，未预热
        ctx = HarnessContext(
            hook=HookKind.PRE_MODEL,
            input={"text": _CLEAN_INPUT},
            loop={"budget_used": 50, "budget_limit": 200_000},
        )

        start = time.perf_counter()
        decision = evaluator.evaluate(hook=HookKind.PRE_MODEL, context=ctx)
        cold_ms = (time.perf_counter() - start) * 1000

        logger.info("harness cold start", extra={"cold_ms": round(cold_ms, 2)})

        assert cold_ms < DEFAULT_TIMEOUT_MS, (
            f"首次求值 {cold_ms:.1f}ms 超过超时上限 {DEFAULT_TIMEOUT_MS}ms，"
            "会被 fail-closed 判成命中。检查是否有内置函数在函数体内惰性 import 重模块。"
        )
        # 良性输入不该被拦：若上面超时，这里会因 fail-closed 而失败
        assert decision.action is not Action.BLOCK, (
            f"良性输入被拦：{decision.message}。"
            "若同时伴随冷启动超时，根因是惰性 import 而非规则表达式。"
        )

    def test_eval_deterministic(self) -> None:
        """验收项 3：同输入多次求值结果一致（确定性）。"""
        evaluator = _build_synthetic_evaluator()
        ctx = HarnessContext(
            hook=HookKind.PRE_MODEL,
            input={"text": _PII_INPUT},
            loop={"budget_used": 50},
        )

        decisions = [
            evaluator.evaluate(hook=HookKind.PRE_MODEL, context=ctx) for _ in range(10)
        ]

        first = decisions[0]
        for d in decisions[1:]:
            assert d.action == first.action
            assert d.message == first.message
            assert len(d.hits) == len(first.hits)
            assert d.winning_hit is not None
            assert first.winning_hit is not None
            assert d.winning_hit.rule.id == first.winning_hit.rule.id
