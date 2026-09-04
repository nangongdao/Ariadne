"""三层预算熔断。

Loop 最大的现实风险是 Token 消耗失控，因此防护必须是**熔断**（强制终止）
而非告警。三层：

| 层级 | 检查点 | 软失败 | 硬失败 |
|---|---|---|---|
| 轮次层 | 每轮开始 | —— | 超 max_iterations → MAX_ITERATIONS |
| 累计层 | 每次 LLM 调用前 | 剩余 < 30% → 降级模型 | 超 total → BUDGET_EXCEEDED |
| 单轮层 | 每次 LLM 调用前 | 上下文超限 → 二次压缩 | 超 per_iteration → 本轮失败 |

计数器放外部存储（Redis）而非进程内存：并行 Loop 共享预算池时，
进程内计数必然超支。成本用**整数微美分**：INCRBYFLOAT 有精度问题，
预算判定必须精确。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Protocol

from ariadne.loop_module.goal import Budget

# 成本以微美分（1e-6 USD）整数存储
MICRO_USD = Decimal("1000000")
# 剩余预算低于此比例时触发降级
DEGRADE_THRESHOLD = 0.3


class BudgetVerdict(StrEnum):
    OK = "ok"
    # 软失败：可继续但应降级
    DEGRADE = "degrade"
    # 硬失败：必须终止
    EXHAUSTED_TOKENS = "exhausted_tokens"
    EXHAUSTED_COST = "exhausted_cost"
    EXCEEDED_ITERATION = "exceeded_iteration"
    EXCEEDED_WALL_CLOCK = "exceeded_wall_clock"

    @property
    def is_fatal(self) -> bool:
        return self not in (BudgetVerdict.OK, BudgetVerdict.DEGRADE)


@dataclass(frozen=True)
class BudgetDecision:
    verdict: BudgetVerdict
    reason: str = ""
    # 预扣的 Token 数，用于后续结算差额
    reserved_tokens: int = 0

    @property
    def allowed(self) -> bool:
        return not self.verdict.is_fatal


@dataclass(frozen=True)
class BudgetUsage:
    total_tokens: int = 0
    cost_micro_usd: int = 0
    iterations: int = 0

    @property
    def cost_usd(self) -> Decimal:
        return Decimal(self.cost_micro_usd) / MICRO_USD


def to_micro_usd(amount: Decimal | float | str) -> int:
    """转微美分整数。向上取整 —— 宁可高估成本也不能低估。

    低估会导致实际花费超出用户设定的上限，那是账单事故。
    """
    value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return int((value * MICRO_USD).to_integral_value(rounding=ROUND_CEILING))


class BudgetCounter(Protocol):
    """预算计数器。

    抽象成 Protocol 是为了让单元测试用内存实现，生产用 Redis ——
    Redis 的原子性是并行 Loop 不超支的前提，但测它不需要真容器。
    """

    def incr_tokens(self, loop_id: str, delta: int) -> int: ...
    def incr_cost(self, loop_id: str, delta_micro: int) -> int: ...
    def incr_iterations(self, loop_id: str, delta: int = 1) -> int: ...
    def get(self, loop_id: str) -> BudgetUsage: ...
    def set_usage(self, loop_id: str, usage: BudgetUsage) -> None: ...
    def reset(self, loop_id: str) -> None: ...


class InMemoryCounter:
    """进程内计数器。仅用于测试与单机单 Loop 场景。

    **不可用于并行 Loop**：多进程/多 Worker 时各自计数会导致总量超支。
    """

    def __init__(self) -> None:
        self._tokens: dict[str, int] = {}
        self._cost: dict[str, int] = {}
        self._iterations: dict[str, int] = {}

    def incr_tokens(self, loop_id: str, delta: int) -> int:
        self._tokens[loop_id] = self._tokens.get(loop_id, 0) + delta
        return self._tokens[loop_id]

    def incr_cost(self, loop_id: str, delta_micro: int) -> int:
        self._cost[loop_id] = self._cost.get(loop_id, 0) + delta_micro
        return self._cost[loop_id]

    def incr_iterations(self, loop_id: str, delta: int = 1) -> int:
        self._iterations[loop_id] = self._iterations.get(loop_id, 0) + delta
        return self._iterations[loop_id]

    def get(self, loop_id: str) -> BudgetUsage:
        return BudgetUsage(
            total_tokens=self._tokens.get(loop_id, 0),
            cost_micro_usd=self._cost.get(loop_id, 0),
            iterations=self._iterations.get(loop_id, 0),
        )

    def set_usage(self, loop_id: str, usage: BudgetUsage) -> None:
        """从检查点恢复。

        **这是最容易出的账单事故**：恢复时不设置累计用量，
        预算就被重置了，Loop 会重新花一遍全部预算。
        """
        self._tokens[loop_id] = usage.total_tokens
        self._cost[loop_id] = usage.cost_micro_usd
        self._iterations[loop_id] = usage.iterations

    def reset(self, loop_id: str) -> None:
        self._tokens.pop(loop_id, None)
        self._cost.pop(loop_id, None)
        self._iterations.pop(loop_id, None)


class BudgetGuard:
    """三层熔断的判定逻辑。

    纯判定 + 计数，不做状态转移 —— 转移由状态机负责。
    """

    def __init__(
        self,
        loop_id: str,
        budget: Budget,
        counter: BudgetCounter,
        *,
        degrade_threshold: float = DEGRADE_THRESHOLD,
    ) -> None:
        self._loop_id = loop_id
        self._budget = budget
        self._counter = counter
        self._degrade_threshold = degrade_threshold

    @property
    def usage(self) -> BudgetUsage:
        return self._counter.get(self._loop_id)

    # ---------- 轮次层 ----------

    def check_iteration(self, next_iteration: int) -> BudgetDecision:
        """轮次层。在 PLANNING 阶段调用 —— 构造上下文前就该知道还能不能跑。"""
        if next_iteration > self._budget.max_iterations:
            return BudgetDecision(
                BudgetVerdict.EXCEEDED_ITERATION,
                f"轮次 {next_iteration} 超过上限 {self._budget.max_iterations}",
            )
        return BudgetDecision(BudgetVerdict.OK)

    def record_iteration(self) -> None:
        """记录一轮已开始。让 BudgetUsage.iterations 准确，前端进度条据此渲染。"""
        self._counter.incr_iterations(self._loop_id)

    def check_wall_clock(self, elapsed_seconds: float) -> BudgetDecision:
        """墙钟超时。由 Worker 看门狗调用，防止卡在不返回的外部调用上。"""
        if elapsed_seconds > self._budget.max_wall_clock_seconds:
            return BudgetDecision(
                BudgetVerdict.EXCEEDED_WALL_CLOCK,
                f"耗时 {elapsed_seconds:.0f}s 超过上限 "
                f"{self._budget.max_wall_clock_seconds}s",
            )
        return BudgetDecision(BudgetVerdict.OK)

    # ---------- 累计层 + 单轮层 ----------

    def reserve(self, estimated_tokens: int) -> BudgetDecision:
        """预扣。返回不允许时调用方必须终止或降级。

        预扣 → 调用 → 结算差额（settle）。先扣后用是关键：
        并发调用时"先查再扣"存在竞态，会导致超支。
        """
        if estimated_tokens < 0:
            raise ValueError("预估 Token 不能为负")

        # 单轮层：单次调用就超单轮上限，直接拒绝
        if estimated_tokens > self._budget.max_tokens_per_iteration:
            return BudgetDecision(
                BudgetVerdict.EXHAUSTED_TOKENS,
                f"单次预估 {estimated_tokens} 超过单轮上限 "
                f"{self._budget.max_tokens_per_iteration}",
            )

        new_total = self._counter.incr_tokens(self._loop_id, estimated_tokens)

        # 累计层硬失败：回滚预扣后拒绝
        if new_total > self._budget.max_total_tokens:
            self._counter.incr_tokens(self._loop_id, -estimated_tokens)
            return BudgetDecision(
                BudgetVerdict.EXHAUSTED_TOKENS,
                f"累计 Token 将达 {new_total}，超过上限 "
                f"{self._budget.max_total_tokens}",
            )

        # 成本已超（上一轮结算后可能已越界）
        usage = self._counter.get(self._loop_id)
        cost_limit = to_micro_usd(self._budget.max_cost_usd)
        if usage.cost_micro_usd > cost_limit:
            self._counter.incr_tokens(self._loop_id, -estimated_tokens)
            return BudgetDecision(
                BudgetVerdict.EXHAUSTED_COST,
                f"累计成本 ${usage.cost_usd} 超过上限 "
                f"${self._budget.max_cost_usd}",
            )

        # 软失败：剩余不足则建议降级到更便宜的模型
        remaining_ratio = 1.0 - new_total / self._budget.max_total_tokens
        cost_ratio = (
            1.0 - usage.cost_micro_usd / cost_limit if cost_limit else 1.0
        )
        if min(remaining_ratio, cost_ratio) < self._degrade_threshold:
            return BudgetDecision(
                BudgetVerdict.DEGRADE,
                f"剩余预算 {min(remaining_ratio, cost_ratio):.0%}，建议降级模型",
                reserved_tokens=estimated_tokens,
            )

        return BudgetDecision(
            BudgetVerdict.OK, reserved_tokens=estimated_tokens
        )

    def settle(
        self, reserved_tokens: int, actual_tokens: int, actual_cost: Decimal
    ) -> BudgetUsage:
        """结算差额。实际用量与预扣的差值加回计数器。"""
        delta = actual_tokens - reserved_tokens
        if delta:
            self._counter.incr_tokens(self._loop_id, delta)
        self._counter.incr_cost(self._loop_id, to_micro_usd(actual_cost))
        return self._counter.get(self._loop_id)

    def release(self, reserved_tokens: int) -> None:
        """调用失败时释放预扣。

        不释放会导致失败的调用也吃预算，多次失败后 Loop 会因
        "预算耗尽"而终止，但实际一个 Token 都没花。
        """
        if reserved_tokens:
            self._counter.incr_tokens(self._loop_id, -reserved_tokens)

    # ---------- 恢复 ----------

    def restore(self, usage: BudgetUsage) -> None:
        """从检查点恢复累计用量。

        崩溃恢复时必须调用 —— 否则预算被重置，Loop 会重新花一遍全部预算。
        这是最容易出的账单事故。
        """
        self._counter.set_usage(self._loop_id, usage)

    def snapshot(self) -> BudgetUsage:
        """当前用量快照，写入检查点。"""
        return self._counter.get(self._loop_id)

    def remaining_tokens(self) -> int:
        return max(self._budget.max_total_tokens - self.usage.total_tokens, 0)

    def remaining_cost_usd(self) -> Decimal:
        limit = to_micro_usd(self._budget.max_cost_usd)
        return max(Decimal(limit - self.usage.cost_micro_usd), Decimal(0)) / MICRO_USD

    def utilization(self) -> dict[str, float]:
        """各维度使用率。用于前端进度条与"是否该提高预算"的判断。"""
        usage = self.usage
        cost_limit = to_micro_usd(self._budget.max_cost_usd)
        return {
            "tokens": round(
                usage.total_tokens / self._budget.max_total_tokens, 4
            ),
            "cost": round(usage.cost_micro_usd / cost_limit, 4) if cost_limit else 0.0,
            "iterations": round(
                usage.iterations / self._budget.max_iterations, 4
            ),
        }
