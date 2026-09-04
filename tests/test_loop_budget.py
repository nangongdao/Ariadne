"""预算熔断测试。

Loop 最大的现实风险是 Token 消耗失控，因此这里的每条测试都对应一种
真实的账单事故模式。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from ariadne.loop_module.budget import (
    BudgetGuard,
    BudgetUsage,
    BudgetVerdict,
    InMemoryCounter,
    to_micro_usd,
)
from ariadne.loop_module.goal import Budget

LOOP = "loop-1"


def guard(**budget_kw: object) -> BudgetGuard:
    defaults: dict[str, object] = {
        "max_iterations": 5,
        "max_total_tokens": 10_000,
        "max_cost_usd": 1.0,
        "max_tokens_per_iteration": 4_000,
        "max_wall_clock_seconds": 300,
    }
    defaults.update(budget_kw)
    return BudgetGuard(LOOP, Budget(**defaults), InMemoryCounter())  # type: ignore[arg-type]


class TestMicroUsd:
    def test_rounds_up_never_down(self) -> None:
        """宁可高估成本也不能低估 —— 低估会导致实际花费超上限。"""
        assert to_micro_usd(Decimal("0.0000001")) == 1
        assert to_micro_usd(Decimal("0.0000019")) == 2

    def test_exact_values_unchanged(self) -> None:
        assert to_micro_usd(Decimal("1.5")) == 1_500_000

    def test_accepts_float_and_str(self) -> None:
        assert to_micro_usd(0.5) == 500_000
        assert to_micro_usd("0.25") == 250_000


class TestIterationLayer:
    def test_within_limit(self) -> None:
        assert guard().check_iteration(3).verdict is BudgetVerdict.OK

    def test_exceeds_limit(self) -> None:
        decision = guard(max_iterations=5).check_iteration(6)
        assert decision.verdict is BudgetVerdict.EXCEEDED_ITERATION
        assert not decision.allowed

    def test_at_limit_is_allowed(self) -> None:
        """第 5 轮在 max_iterations=5 时应允许 —— 边界是"超过"才拒。"""
        assert guard(max_iterations=5).check_iteration(5).allowed


class TestWallClock:
    def test_within_limit(self) -> None:
        assert guard(max_wall_clock_seconds=300).check_wall_clock(100).allowed

    def test_timeout_is_fatal(self) -> None:
        decision = guard(max_wall_clock_seconds=60).check_wall_clock(61)
        assert decision.verdict is BudgetVerdict.EXCEEDED_WALL_CLOCK
        assert not decision.allowed


class TestReserveSettle:
    def test_reserve_then_settle_tracks_actual(self) -> None:
        g = guard()
        decision = g.reserve(1000)
        assert decision.allowed
        assert decision.reserved_tokens == 1000

        # 实际用了 1200，差额补上
        usage = g.settle(1000, 1200, Decimal("0.05"))
        assert usage.total_tokens == 1200
        assert usage.cost_usd == Decimal("0.05")

    def test_settle_with_less_than_reserved(self) -> None:
        g = guard()
        g.reserve(2000)
        usage = g.settle(2000, 800, Decimal("0.01"))
        assert usage.total_tokens == 800

    def test_reserve_before_use_prevents_race(self) -> None:
        """先扣后用：两次并发预扣的总量必须都被计入。

        "先查再扣"会让两个并发调用都通过检查，导致超支。
        """
        # 单轮上限设为与总量相同，隔离出累计层的行为
        g = guard(max_total_tokens=10_000, max_tokens_per_iteration=10_000)
        first = g.reserve(6000)
        second = g.reserve(6000)
        assert first.allowed
        assert not second.allowed, "第二次预扣应因累计超限被拒"
        # 被拒的预扣已回滚
        assert g.usage.total_tokens == 6000

    def test_release_on_failure(self) -> None:
        """调用失败必须释放预扣 —— 否则失败的调用也吃预算。"""
        g = guard()
        decision = g.reserve(3000)
        g.release(decision.reserved_tokens)
        assert g.usage.total_tokens == 0

    def test_repeated_failures_do_not_exhaust_budget(self) -> None:
        """多次失败后仍应有预算 —— 实际一个 Token 都没花。"""
        g = guard(max_total_tokens=10_000)
        for _ in range(10):
            decision = g.reserve(3000)
            assert decision.allowed
            g.release(decision.reserved_tokens)
        assert g.remaining_tokens() == 10_000

    def test_negative_estimate_rejected(self) -> None:
        with pytest.raises(ValueError, match="不能为负"):
            guard().reserve(-100)


class TestHardLimits:
    def test_cumulative_token_limit(self) -> None:
        g = guard(max_total_tokens=5000, max_tokens_per_iteration=5000)
        g.settle(0, 4500, Decimal("0"))
        decision = g.reserve(1000)
        assert decision.verdict is BudgetVerdict.EXHAUSTED_TOKENS
        assert not decision.allowed

    def test_single_call_over_per_iteration_limit(self) -> None:
        decision = guard(max_tokens_per_iteration=4000).reserve(5000)
        assert decision.verdict is BudgetVerdict.EXHAUSTED_TOKENS
        assert "单轮上限" in decision.reason

    def test_cost_limit_enforced(self) -> None:
        g = guard(max_cost_usd=0.10)
        g.settle(0, 100, Decimal("0.15"))
        decision = g.reserve(100)
        assert decision.verdict is BudgetVerdict.EXHAUSTED_COST
        assert not decision.allowed

    def test_rejected_reserve_does_not_leak_tokens(self) -> None:
        """被拒的预扣必须回滚，否则计数器会虚高。"""
        g = guard(max_total_tokens=1000, max_tokens_per_iteration=1000)
        g.settle(0, 900, Decimal("0"))
        before = g.usage.total_tokens
        g.reserve(500)
        assert g.usage.total_tokens == before


class TestDegradation:
    def test_low_remaining_triggers_degrade_not_termination(self) -> None:
        """软失败：仍可继续但应换便宜模型。"""
        g = guard(max_total_tokens=10_000)
        g.settle(0, 7500, Decimal("0"))
        decision = g.reserve(100)
        assert decision.verdict is BudgetVerdict.DEGRADE
        assert decision.allowed, "降级不应终止 Loop"
        assert decision.reserved_tokens == 100

    def test_low_cost_remaining_also_degrades(self) -> None:
        g = guard(max_cost_usd=1.0)
        g.settle(0, 10, Decimal("0.80"))
        assert guard is not None
        decision = g.reserve(10)
        assert decision.verdict is BudgetVerdict.DEGRADE

    def test_degrade_verdict_is_not_fatal(self) -> None:
        assert not BudgetVerdict.DEGRADE.is_fatal
        assert BudgetVerdict.EXHAUSTED_TOKENS.is_fatal


class TestCheckpointRestore:
    def test_restore_prevents_budget_reset(self) -> None:
        """**最容易出的账单事故**：崩溃恢复时不恢复用量，
        预算被重置，Loop 重新花一遍全部预算。"""
        original = guard(max_total_tokens=10_000)
        original.settle(0, 8000, Decimal("0.80"))
        snapshot = original.snapshot()

        # 模拟另一个 Worker 接管
        resumed = guard(max_total_tokens=10_000)
        resumed.restore(snapshot)

        assert resumed.usage.total_tokens == 8000
        assert resumed.remaining_tokens() == 2000
        # 恢复后立刻大额预扣应被拒
        assert not resumed.reserve(3000).allowed

    def test_without_restore_budget_is_reset(self) -> None:
        """反证：不调用 restore 就会重置 —— 说明这个调用是必需的。"""
        resumed = guard(max_total_tokens=10_000)
        assert resumed.remaining_tokens() == 10_000

    def test_snapshot_roundtrip(self) -> None:
        g = guard()
        g.settle(0, 1234, Decimal("0.0567"))
        snapshot = g.snapshot()
        assert snapshot.total_tokens == 1234
        assert snapshot.cost_usd == Decimal("0.0567")

    def test_restore_includes_iterations(self) -> None:
        resumed = guard()
        resumed.restore(BudgetUsage(total_tokens=100, cost_micro_usd=1000, iterations=3))
        assert resumed.usage.iterations == 3


class TestReporting:
    def test_remaining_never_negative(self) -> None:
        g = guard(max_total_tokens=1000)
        g.settle(0, 5000, Decimal("0"))
        assert g.remaining_tokens() == 0

    def test_remaining_cost_never_negative(self) -> None:
        g = guard(max_cost_usd=0.10)
        g.settle(0, 0, Decimal("0.50"))
        assert g.remaining_cost_usd() == Decimal("0")

    def test_utilization_all_dimensions(self) -> None:
        g = guard(max_total_tokens=10_000, max_cost_usd=1.0, max_iterations=10)
        g.settle(0, 2500, Decimal("0.40"))
        util = g.utilization()
        assert util["tokens"] == 0.25
        assert util["cost"] == 0.40
        assert util["iterations"] == 0.0


class TestCounterIsolation:
    def test_separate_loops_do_not_share(self) -> None:
        counter = InMemoryCounter()
        budget = Budget(max_total_tokens=1000, max_tokens_per_iteration=1000)
        a = BudgetGuard("loop-a", budget, counter)
        b = BudgetGuard("loop-b", budget, counter)

        a.settle(0, 900, Decimal("0"))
        assert b.usage.total_tokens == 0
        assert b.reserve(500).allowed

    def test_reset_clears_only_target(self) -> None:
        counter = InMemoryCounter()
        counter.incr_tokens("a", 100)
        counter.incr_tokens("b", 200)
        counter.reset("a")
        assert counter.get("a").total_tokens == 0
        assert counter.get("b").total_tokens == 200
