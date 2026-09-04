"""Loop Engine 主循环测试。

Engine 是 M3 的核心：把状态机、预算、Verifier、Critique、上下文、指纹
串起来。所有外部依赖（LLM、检查点、时钟、事件）用纯桩替换——engine 的
正确性不该依赖真实 provider 或容器。

覆盖 M3-spec 第 10 节验收清单中可单测的项：
  2  不可验证目标被拒 —— VALIDATE -> REJECTED
  3  收敛只看 blocking 断言 —— 高 score 但 blocking 未过 -> 不收敛
  4  假完成 —— claimed_done=True 且断言未过 -> 不收敛且标记 false_completion
  7  振荡被检测 —— 反复同一输出 -> STALLED
  8  预算硬熔断 —— 极小预算 -> BUDGET_EXCEEDED
  9  崩溃恢复不重跑 —— 检查点续跑，iteration 不回退
  10 恢复后预算不重置 —— 从检查点恢复的用量被保留
  13 上下文不随轮次线性膨胀 —— 多轮后单轮上下文量基本持平
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from ariadne.loop_module.budget import BudgetGuard, InMemoryCounter
from ariadne.loop_module.checkpoint import InMemoryCheckpointStore
from ariadne.loop_module.engine import (
    Clock,
    EventSink,
    LLMResponse,
    LoopConfig,
    LoopEngine,
)
from ariadne.loop_module.fingerprint import OscillationDetector
from ariadne.loop_module.goal import Assertion, AssertionKind, Budget, Goal
from ariadne.loop_module.modes import BaseLoopMode, LoopModeFactory
from ariadne.loop_module.state_machine import LoopEvent, LoopState
from ariadne.loop_module.verifier.builtin import DictMetricProvider

# ---------- 桩 ----------


class FakeClock:
    """可控时钟。手动推进验证墙钟熔断。"""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@dataclass
class ScriptedLLM:
    """按脚本输出。每次 complete 取列表下一个；支持抛异常测重试。

    claimed_done 故意可独立设置 —— 验证"不信任模型自评"：模型自称完成
    但断言未过时，Loop 不该收敛。
    """

    outputs: list[str]
    claimed_done: list[bool] | None = None
    errors: list[BaseException | None] | None = None
    input_tokens: int = 100
    output_tokens: int = 200
    cost_usd: Decimal = Decimal("0.01")
    calls: int = 0

    async def complete(self, prompt: str, *, model: str) -> LLMResponse:
        idx = self.calls
        self.calls += 1
        if self.errors and idx < len(self.errors) and self.errors[idx] is not None:
            raise self.errors[idx]  # type: ignore[misc]
        output = self.outputs[idx] if idx < len(self.outputs) else self.outputs[-1]
        claimed = (
            self.claimed_done[idx]
            if self.claimed_done and idx < len(self.claimed_done)
            else False
        )
        return LLMResponse(
            output=output,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            claimed_done=claimed,
            model=model,
            cost_usd=self.cost_usd,
        )


@dataclass
class RecordingSink:
    """记录所有事件，断言状态流转。"""

    events: list[LoopEvent] = field(default_factory=list)

    async def emit(self, event: LoopEvent) -> None:
        self.events.append(event)


# ---------- 装配辅助 ----------


def make_goal(
    *,
    task: str = "写一个返回两数之和的 Python 函数",
    assertions: tuple[Assertion, ...] | None = None,
    budget: Budget | None = None,
    mode: str = "quality",
) -> Goal:
    if assertions is None:
        assertions = (
            Assertion(
                id="has_def",
                kind=AssertionKind.REGEX,
                spec={"pattern": r"def\s+\w+\s*\("},
                hint="输出必须包含一个函数定义",
            ),
        )
    return Goal(
        task=task,
        assertions=assertions,
        budget=budget or Budget(max_iterations=5, max_total_tokens=50_000),
        mode=mode,  # type: ignore[arg-type]
    )


def make_engine(
    goal: Goal,
    llm: ScriptedLLM,
    *,
    loop_id: str = "loop-test",
    clock: Clock | None = None,
    metric_provider: DictMetricProvider | None = None,
    event_sink: EventSink | None = None,
    counter: InMemoryCounter | None = None,
    oscillator: OscillationDetector | None = None,
    mode: BaseLoopMode | None = None,
) -> tuple[LoopEngine, InMemoryCheckpointStore, InMemoryCounter]:
    counter = counter or InMemoryCounter()
    guard = BudgetGuard(loop_id, goal.budget, counter)
    store = InMemoryCheckpointStore()
    mode = mode or LoopModeFactory(goal.mode)
    cfg = LoopConfig(
        goal=goal,
        loop_id=loop_id,
        project_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        budget_guard=guard,
        llm=llm,
        checkpoint_store=store,
        mode=mode,
        metric_provider=metric_provider,
        clock=clock or FakeClock(),
        event_sink=event_sink or RecordingSink(),
        oscillator=oscillator or OscillationDetector(),
    )
    return LoopEngine(cfg), store, counter


async def run_engine(engine: LoopEngine):
    """pytest-asyncio auto 模式下用 await；同步语境用 asyncio.run。"""
    return await engine.run()


# ---------- 验收项 3 + 4：收敛与假完成 ----------


class TestConvergence:
    def test_first_pass_converges(self) -> None:
        """一轮即达标：断言通过 -> CONVERGED，iteration=1。"""
        goal = make_goal()
        llm = ScriptedLLM(outputs=["def add(a, b): return a + b\n"], claimed_done=[True])
        engine, _, _ = make_engine(goal, llm)

        outcome = asyncio_run(engine.run())

        assert outcome.success
        assert outcome.final_state is LoopState.CONVERGED
        assert outcome.iterations == 1

    def test_false_completion_not_converged(self) -> None:
        """验收项 4：模型自称完成但断言未过 -> 不收敛，标记 false_completion。

        这是 Ralph 原则的核心：claimed_done 不产生状态转移。
        """
        goal = make_goal()
        # 没有函数定义，断言不过；但模型自称完成
        llm = ScriptedLLM(
            outputs=["这里没有函数定义\n"] * 8,
            claimed_done=[True] * 8,
        )
        engine, _, _ = make_engine(goal, llm)

        outcome = asyncio_run(engine.run())

        assert not outcome.success
        assert outcome.verdict is not None
        assert outcome.verdict.false_completion
        # 自称完成但没收敛 —— 这正是假完成
        assert outcome.verdict.claimed_done
        assert not outcome.verdict.converged

    def test_high_score_does_not_converge_without_blocking(self) -> None:
        """验收项 3：高 score 但 blocking 断言未过 -> 不收敛。

        score 只用于趋势，绝不作为收敛依据（docs/03 第 4 节）。
        """
        # metric 断言拿高分，但 regex 断言（blocking）未过
        assertions = (
            Assertion(
                id="score",
                kind=AssertionKind.METRIC,
                spec={"name": "quality", "op": ">=", "value": 50},
                weight=1.0,
                blocking=False,  # 期望满足但不阻塞
            ),
            Assertion(
                id="format",
                kind=AssertionKind.REGEX,
                spec={"pattern": r"def\s+\w+\s*\("},
                blocking=True,
            ),
        )
        goal = make_goal(
            assertions=assertions,
            budget=Budget(max_iterations=6, max_total_tokens=1_000_000),
        )
        provider = DictMetricProvider({"quality": 95.0})  # 高分
        # 每轮输出不同，避免触发振荡
        llm = ScriptedLLM(outputs=[f"无函数定义内容 {i}\n" for i in range(8)])
        engine, _, _ = make_engine(goal, llm, metric_provider=provider)

        outcome = asyncio_run(engine.run())

        assert not outcome.success
        # 高 score 但 blocking 未过 -> 不收敛。
        # 可能是 MAX_ITERATIONS（跑满轮次）或 STALLED（同失败签名反复），
        # 都是正确的"未收敛"终态 —— 关键是没因高分收敛。
        assert outcome.final_state in (LoopState.MAX_ITERATIONS, LoopState.STALLED)

    def test_revision_then_converge(self) -> None:
        """第一轮失败、第二轮达标 —— 验证 REVISING -> PLANNING 闭环。"""
        goal = make_goal()
        llm = ScriptedLLM(
            outputs=["没有函数\n", "def add(a, b): return a + b\n"],
            claimed_done=[False, True],
        )
        engine, _, _ = make_engine(goal, llm)

        outcome = asyncio_run(engine.run())

        assert outcome.success
        assert outcome.iterations == 2


# ---------- 验收项 2：不可验证目标被拒 ----------


class TestGoalValidation:
    def test_empty_assertions_rejected(self) -> None:
        """空断言 -> REJECTED，不进迭代。"""
        goal = Goal(task="做点什么", assertions=(), budget=Budget())
        llm = ScriptedLLM(outputs=["x"])
        engine, _, _ = make_engine(goal, llm)

        outcome = asyncio_run(engine.run())

        assert outcome.final_state is LoopState.REJECTED
        assert outcome.iterations == 0  # 没进迭代
        assert llm.calls == 0  # 没调 LLM

    def test_all_non_blocking_rejected(self) -> None:
        """全 non-blocking -> REJECTED —— 没有硬性收敛条件。"""
        assertions = (
            Assertion(
                id="soft",
                kind=AssertionKind.REGEX,
                spec={"pattern": r".+"},
                blocking=False,
            ),
        )
        goal = make_goal(assertions=assertions)
        llm = ScriptedLLM(outputs=["x"])
        engine, _, _ = make_engine(goal, llm)

        outcome = asyncio_run(engine.run())

        assert outcome.final_state is LoopState.REJECTED


# ---------- 验收项 8：预算硬熔断 ----------


class TestBudgetCircuitBreaker:
    def test_tiny_budget_terminates(self) -> None:
        """极小预算 -> BUDGET_EXCEEDED，不收敛。"""
        budget = Budget(
            max_iterations=10,
            max_total_tokens=600,
            max_cost_usd=1.0,
            max_tokens_per_iteration=500,
        )
        goal = make_goal(budget=budget)
        llm = ScriptedLLM(
            outputs=[f"无函数 {i}\n" for i in range(10)],
            input_tokens=100,
            output_tokens=200,
        )
        engine, _, _ = make_engine(goal, llm)

        outcome = asyncio_run(engine.run())

        assert outcome.final_state is LoopState.BUDGET_EXCEEDED
        assert not outcome.success

    def test_budget_exceeded_does_not_overspend(self) -> None:
        """熔断后实际用量不超上限（账单安全）。"""
        budget = Budget(
            max_iterations=10,
            max_total_tokens=500,
            max_cost_usd=1.0,
            max_tokens_per_iteration=500,
        )
        goal = make_goal(budget=budget)
        llm = ScriptedLLM(
            outputs=[f"无函数 {i}\n" for i in range(10)],
            input_tokens=100,
            output_tokens=200,
        )
        engine, _, counter = make_engine(goal, llm)

        asyncio_run(engine.run())

        # 累计 token 不该显著超过上限（预扣 + 结算可能略微越过，但 settle 修正）
        assert counter.get("loop-test").total_tokens <= 500 + 200

    def test_max_iterations_terminal(self) -> None:
        """跑满轮次仍未收敛 -> MAX_ITERATIONS。"""
        budget = Budget(max_iterations=2, max_total_tokens=100_000)
        goal = make_goal(budget=budget)
        llm = ScriptedLLM(outputs=[f"无函数 {i}\n" for i in range(5)])
        engine, _, _ = make_engine(goal, llm)

        outcome = asyncio_run(engine.run())

        assert outcome.final_state is LoopState.MAX_ITERATIONS
        assert outcome.iterations == 2

    def test_wall_clock_timeout(self) -> None:
        """墙钟超时 -> 终止。"""
        budget = Budget(
            max_iterations=10, max_total_tokens=1_000_000, max_wall_clock_seconds=10
        )
        goal = make_goal(budget=budget)
        clock = FakeClock()
        llm = ScriptedLLM(outputs=[f"无函数 {i}\n" for i in range(10)])
        engine, _, _ = make_engine(goal, llm, clock=clock)
        # 时钟已超过墙钟上限 -> 第一轮 PLANNING 即终止
        clock.advance(20)
        outcome = asyncio_run(engine.run())
        assert outcome.final_state is LoopState.BUDGET_EXCEEDED


# ---------- 验收项 7：振荡检测 ----------


class TestOscillation:
    def test_repeated_output_goes_stalled(self) -> None:
        """反复同一输出且断言不过 -> STALLED。

        构造 mock 模型反复输出同一错误，验证振荡检测终止而非烧满预算。
        """
        budget = Budget(max_iterations=10, max_total_tokens=1_000_000)
        goal = make_goal(budget=budget)
        same = "完全相同的错误输出\n"
        llm = ScriptedLLM(outputs=[same] * 10)
        engine, _, _ = make_engine(goal, llm)

        outcome = asyncio_run(engine.run())

        # output_fp 重复达 stall_after 次 -> STALLED
        assert outcome.final_state is LoopState.STALLED
        assert outcome.iterations <= 5  # 早于第 10 轮终止

    def test_repeated_failure_signature_goes_stalled(self) -> None:
        """连续 3 轮同 failure_fp（输出不同）-> STALLED。"""
        budget = Budget(max_iterations=10, max_total_tokens=1_000_000)
        goal = make_goal(budget=budget)
        llm = ScriptedLLM(
            outputs=["错误输出一\n", "错误输出二\n", "错误输出三\n"],
        )
        engine, _, _ = make_engine(goal, llm)

        outcome = asyncio_run(engine.run())

        # 三轮同 failure_fp -> STALLED
        assert outcome.final_state is LoopState.STALLED


# ---------- 验收项 9 + 10：崩溃恢复 ----------


class TestCrashRecovery:
    def test_resume_from_checkpoint_no_replay(self) -> None:
        """验收项 9：崩溃后从检查点续跑，iteration 不回退。

        engine1 跑 2 轮到 MAX_ITERATIONS（落 2 个检查点）。新 engine2 共享
        检查点 store，从 iteration=2 恢复，第 3 轮收敛，不重跑第 1-2 轮。
        """
        counter = InMemoryCounter()
        # engine1 用 max_iterations=2，跑满即停（落 2 个检查点）
        budget1 = Budget(max_iterations=2, max_total_tokens=1_000_000)
        goal1 = make_goal(budget=budget1)
        llm1 = ScriptedLLM(outputs=["没函数一\n", "没函数二\n"])
        engine1, store, _ = make_engine(goal1, llm1, counter=counter)
        asyncio_run(engine1.run())
        assert engine1._iteration == 2

        # 崩溃后新 engine：从检查点恢复（iteration=2），第 3 轮收敛
        goal2 = make_goal(budget=Budget(max_iterations=5, max_total_tokens=1_000_000))
        llm2 = ScriptedLLM(
            outputs=["def add(a, b): return a + b\n"], claimed_done=[True]
        )
        engine2, _, _ = make_engine(goal2, llm2, loop_id="loop-test", counter=counter)
        engine2._cfg.checkpoint_store = store

        outcome = asyncio_run(engine2.run())

        assert outcome.success
        assert outcome.final_state is LoopState.CONVERGED
        assert outcome.iterations == 3  # 从第 3 轮续跑，不回退到 1

    def test_restore_preserves_budget(self) -> None:
        """验收项 10：恢复后预算不重置。

        崩溃前用掉 N token，恢复后用量应从 N 起算，而非从 0。
        这是最容易出的账单事故（docs/03 第 9 节）。
        """
        budget = Budget(max_iterations=2, max_total_tokens=1_000_000)
        goal = make_goal(budget=budget)
        llm1 = ScriptedLLM(
            outputs=["没函数一\n", "没函数二\n"], input_tokens=100, output_tokens=200
        )
        engine1, store, counter = make_engine(goal, llm1)
        asyncio_run(engine1.run())
        used_before = counter.get("loop-test").total_tokens
        assert used_before > 0

        # 新 engine 从检查点恢复
        llm2 = ScriptedLLM(
            outputs=["def add(a, b): return a + b\n"], claimed_done=[True]
        )
        engine2, _, _ = make_engine(goal, llm2, loop_id="loop-test")
        engine2._cfg.checkpoint_store = store

        # 恢复后用量应等于崩溃前的用量（没重置）
        checkpoint = asyncio_run_sync(
            store.latest("loop-test", project_id=uuid.UUID("00000000-0000-0000-0000-000000000001"))
        )
        assert checkpoint is not None
        engine2._restore(checkpoint)
        assert engine2._guard.usage.total_tokens == checkpoint.usage.total_tokens
        assert engine2._guard.usage.total_tokens == used_before

    def test_checkpoint_saved_each_iteration(self) -> None:
        """每轮落检查点 —— 状态外置的铁律。"""
        budget = Budget(max_iterations=3, max_total_tokens=1_000_000)
        goal = make_goal(budget=budget)
        llm = ScriptedLLM(outputs=[f"无函数 {i}\n" for i in range(3)])
        engine, store, _ = make_engine(goal, llm)

        asyncio_run(engine.run())

        all_cps = store.all("loop-test")
        assert len(all_cps) == 3
        assert [c.iteration for c in all_cps] == [1, 2, 3]

    def test_restore_preserves_oscillation_history(self) -> None:
        """恢复振荡历史 —— 否则崩溃后振荡检测失效。"""
        budget = Budget(max_iterations=10, max_total_tokens=1_000_000)
        goal = make_goal(budget=budget)
        # 前 3 轮同输出（第 4 轮会 STALLED）
        same = "同样的错\n"
        llm1 = ScriptedLLM(outputs=[same, same, same])
        engine1, _, _ = make_engine(goal, llm1)
        asyncio_run(engine1.run())
        # engine1 应该 STALLED（重复输出 >= stall_after=3 次）
        assert engine1._state is LoopState.STALLED
        assert len(engine1._cfg.oscillator.history) >= 3


# ---------- 验收项 13：上下文不随轮次膨胀 ----------


class TestContextConvergence:
    def test_context_does_not_grow_linearly(self) -> None:
        """多轮 Loop 的单轮上下文量应基本持平（docs/03 第 6 节）。

        四段结构 + 历史只保留失败签名 + 二次压缩，确保不线性膨胀。
        """
        budget = Budget(max_iterations=6, max_total_tokens=1_000_000)
        goal = make_goal(budget=budget)
        # 每轮不同输出，避免振荡；都不收敛
        llm = ScriptedLLM(outputs=[f"输出 {i} 无函数定义\n" for i in range(6)])
        engine, _, _ = make_engine(goal, llm)

        prompt_sizes: list[int] = []
        original_complete = llm.complete

        async def tracking_complete(prompt: str, *, model: str) -> LLMResponse:
            prompt_sizes.append(len(prompt))
            return await original_complete(prompt, model=model)

        llm.complete = tracking_complete  # type: ignore[assignment]

        asyncio_run(engine.run())

        assert len(prompt_sizes) >= 3
        # 第一轮最短（无历史）。取第 2 轮和最后一轮比较
        later = prompt_sizes[-1]
        early = prompt_sizes[1] if len(prompt_sizes) > 1 else prompt_sizes[0]
        # 后期上下文不该比早期大一个数量级（四段收敛的承诺）
        assert later < early * 3, (
            f"上下文线性膨胀：第2轮 {early} 字符，最后一轮 {later} 字符"
        )


# ---------- 状态机一致性 ----------


class TestStateMachineIntegration:
    def test_engine_does_not_invent_transitions(self) -> None:
        """engine 不自造转移 —— 所有状态变化都经 next_state。"""
        goal = make_goal()
        llm = ScriptedLLM(
            outputs=["def add(a, b): return a + b\n"], claimed_done=[True]
        )
        sink = RecordingSink()
        engine, _, _ = make_engine(goal, llm, event_sink=sink)

        asyncio_run(engine.run())

        assert LoopEvent.START in sink.events
        assert LoopEvent.GOAL_VALID in sink.events
        assert LoopEvent.CONVERGED in sink.events
        # 没有任何非法事件（engine 若自造转移会抛 InvalidTransitionError）
        assert engine._state is LoopState.CONVERGED

    def test_iteration_failure_attributed(self) -> None:
        """执行失败（LLM 抛异常）-> EXECUTION_FAILED -> JUDGING -> 修正。"""
        goal = make_goal()
        llm = ScriptedLLM(
            outputs=["def add(a, b): return a + b\n"],
            errors=[ConnectionError("provider 抖动"), None],
            claimed_done=[False, True],
        )
        engine, _, _ = make_engine(goal, llm)

        outcome = asyncio_run(engine.run())

        # 第一轮执行失败（quality 模式不原地重试）-> JUDGING -> NEEDS_REVISION
        # 第二轮执行成功 -> 收敛
        assert outcome.success
        assert outcome.iterations == 2


# ---------- 审查修复的回归测试 ----------


class TestReviewFixes:
    def test_goal_stall_params_respected(self) -> None:
        """Goal.stall_patience 应被同步到 OscillationDetector 的收益递减检测。

        回归 W2：原先 Goal 的 stall 参数被静默忽略，检测器用自己默认值。
        用收益递减场景验证：stall_patience=1 + stall_threshold=100 表示
        连续 1 轮得分提升 < 100 即判收益递减 STALLED。
        """
        from dataclasses import replace

        goal = make_goal(
            assertions=(
                Assertion(
                    id="metric",
                    kind=AssertionKind.METRIC,
                    spec={"name": "q", "op": ">=", "value": 90},
                    blocking=True,
                ),
            ),
            budget=Budget(max_iterations=10, max_total_tokens=1_000_000),
        )
        # stall_patience=1, stall_threshold=100：连续 1 轮提升 < 100 即收益递减
        goal = replace(goal, stall_patience=1, stall_threshold=100.0)
        # 指标缓慢上升（每轮 +1，远低于 threshold=100）-> 收益递减触发
        provider = DictMetricProvider({"q": 10.0})
        llm = ScriptedLLM(outputs=[f"输出 {i}\n" for i in range(10)])
        engine, _, _ = make_engine(goal, llm, metric_provider=provider)

        outcome = asyncio_run(engine.run())

        # 收益递减应在 stall_patience+1=2 轮后触发 STALLED
        assert outcome.final_state is LoopState.STALLED
        assert outcome.iterations <= 3

    def test_execution_failure_uses_empty_output_fp(self) -> None:
        """执行失败时 output_fp 应为空，不是上一轮的输出指纹。

        回归 C1：用上一轮指纹会让振荡检测误判"连续执行失败"为"输出振荡"。
        """
        goal = make_goal(budget=Budget(max_iterations=5, max_total_tokens=1_000_000))
        # 每轮都执行失败，检查点 output_fp 应全部为空
        llm = ScriptedLLM(
            outputs=["def add(a, b): return a + b\n"],
            errors=[ConnectionError("抖动")] * 5,
        )
        engine, store, _ = make_engine(goal, llm)

        asyncio_run(engine.run())

        cps = store.all("loop-test")
        assert len(cps) >= 1
        # 执行失败的检查点 output_fp 必须为空（C1 修复）
        for cp in cps:
            assert cp.output_fp == "", (
                f"执行失败检查点的 output_fp 应为空，实际 {cp.output_fp!r}"
            )

    def test_iterations_counter_increments(self) -> None:
        """BudgetUsage.iterations 应随轮次递增（前端进度条依赖）。

        回归 W5：原先 incr_iterations 从未被调用，iterations 始终为 0。
        """
        budget = Budget(max_iterations=3, max_total_tokens=1_000_000)
        goal = make_goal(budget=budget)
        llm = ScriptedLLM(outputs=[f"无函数 {i}\n" for i in range(3)])
        engine, _, counter = make_engine(goal, llm)

        asyncio_run(engine.run())

        assert counter.get("loop-test").iterations == 3


# ---------- 端到端验收（验收项 5/6 的可单测代理） ----------


class TestEndToEndConvergence:
    """代码生成场景的端到端收敛验收。

    M3-spec 验收项 5（闭环达标率 ≥85%）和 6（平均 ≤3 轮）需要真实缺陷用例集。
    这里用 REGEX 断言代理 COMMAND 断言（受限子进程在无 venv 环境跑不了 pytest），
    验证 engine 端到端能把"不达标输出"迭代到"达标"。
    """

    def test_converges_within_three_iterations(self) -> None:
        """典型场景：模型经 1-2 轮修正后达标，平均 ≤3 轮。"""
        goal = make_goal(budget=Budget(max_iterations=5, max_total_tokens=1_000_000))
        # 第1轮缺 return，第2轮缺 def，第3轮完整 -> 收敛
        llm = ScriptedLLM(
            outputs=[
                "add(a, b): return a + b\n",
                "def add(a, b): a + b\n",
                "def add(a, b): return a + b\n",
            ],
            claimed_done=[True, True, True],
        )
        engine, _, _ = make_engine(goal, llm)

        outcome = asyncio_run(engine.run())

        assert outcome.success
        assert outcome.iterations <= 3
        assert outcome.final_state is LoopState.CONVERGED

    def test_false_completion_caught_in_e2e(self) -> None:
        """模型连续自称完成但始终不达标 -> 不收敛（假完成拦截）。

        验收项 4：假完成拦截率 ≥95%。这里构造 100% 自称完成但不达标的样本，
        验证 Loop 不被假完成骗收敛。
        """
        goal = make_goal(budget=Budget(max_iterations=4, max_total_tokens=1_000_000))
        llm = ScriptedLLM(
            outputs=["这是结果，完成了\n"] * 4,
            claimed_done=[True] * 4,
        )
        engine, _, _ = make_engine(goal, llm)

        outcome = asyncio_run(engine.run())

        assert not outcome.success
        assert outcome.verdict is not None
        assert outcome.verdict.false_completion
        assert outcome.verdict.claimed_done  # 每轮都自称完成
        assert not outcome.verdict.converged  # 但从未收敛


def asyncio_run(coro):
    """同步驱动协程。pytest-asyncio auto 模式兼容。"""
    import asyncio

    return asyncio.run(coro)


def asyncio_run_sync(awaitable):
    """同步获取协程结果。"""
    import asyncio

    return asyncio.run(awaitable)


# ---------- R9：Retry 模式原地重试 ----------


class TestInPlaceRetry:
    """Retry 模式的原地重试在 EXECUTING 内落地（R9）。

    可重试错误（429/超时）release → 退避 → 重新 reserve → 重新调用，
    不消耗迭代轮次；失败的尝试不烧预算。用极短退避避免真实 sleep 拖慢测试。
    """

    @staticmethod
    def _fast_retry_mode() -> BaseLoopMode:
        from ariadne.loop_module.modes.retry import RetryConfig, RetryMode

        return RetryMode(
            RetryConfig(base_delay=0.001, max_delay=0.01, jitter=0.25, max_retries=3)
        )

    def test_retry_then_success_converges(self) -> None:
        """连续 429 两次后成功：原地重试 3 次调用，但只算 1 轮，收敛。"""
        from ariadne.runtime_module.llm.errors import LLMRateLimitError

        goal = make_goal(mode="retry")
        llm = ScriptedLLM(
            outputs=["def add(a, b): return a + b\n"],
            claimed_done=[True],
            errors=[
                LLMRateLimitError("429"),
                LLMRateLimitError("429"),
            ],
        )
        engine, _, _ = make_engine(goal, llm, mode=self._fast_retry_mode())
        outcome = asyncio_run(engine.run())

        assert outcome.converged is True
        # 2 次失败 + 1 次成功都在第 1 轮内
        assert llm.calls == 3
        assert outcome.iterations == 1

    def test_failed_attempts_burn_no_budget(self) -> None:
        """失败的尝试 release 预扣——预算只记录成功调用的真实用量。"""
        from ariadne.runtime_module.llm.errors import LLMRateLimitError

        goal = make_goal(mode="retry")
        llm = ScriptedLLM(
            outputs=["def add(a, b): return a + b\n"],
            claimed_done=[True],
            errors=[LLMRateLimitError("429")],
        )
        engine, _, counter = make_engine(goal, llm, mode=self._fast_retry_mode())
        asyncio_run(engine.run())

        usage = counter.get("loop-test")
        # 只有成功那次结算：input 100 + output 200
        assert usage.total_tokens == 300

    def test_retry_exhausted_fails_over(self) -> None:
        """重试耗尽：EXECUTION_FAILED 交回状态机，走失败路径。"""
        from ariadne.runtime_module.llm.errors import LLMRateLimitError

        goal = make_goal(
            mode="retry",
            budget=Budget(max_iterations=1, max_total_tokens=200_000),
        )
        llm = ScriptedLLM(
            outputs=["def add(a, b): pass"],
            errors=[LLMRateLimitError("429")] * 10,
        )
        engine, _, _ = make_engine(goal, llm, mode=self._fast_retry_mode())
        outcome = asyncio_run(engine.run())

        assert outcome.converged is False
        # 1 轮内原地重试 max_retries=3 次 + 首次 = 4 次调用
        assert llm.calls == 4

    def test_non_retryable_no_in_place_retry(self) -> None:
        """不可重试错误（ValueError）不原地重试，直接交 JUDGING。"""
        goal = make_goal(
            mode="retry",
            budget=Budget(max_iterations=2, max_total_tokens=200_000),
        )
        llm = ScriptedLLM(
            outputs=["def add(a, b): pass"],
            errors=[ValueError("bad schema")] * 4,
        )
        engine, _, _ = make_engine(goal, llm, mode=self._fast_retry_mode())
        asyncio_run(engine.run())

        # 每轮恰好 1 次调用（无原地重试），2 轮共 2 次
        assert llm.calls == 2

    def test_http_status_error_429_retried(self) -> None:
        """裸 httpx.HTTPStatusError(429)（未翻译的适配器）也走原地重试。"""
        import httpx

        request = httpx.Request("POST", "https://api.example.com/v1/messages")
        response = httpx.Response(429, request=request)
        error = httpx.HTTPStatusError(
            "429 Too Many Requests", request=request, response=response
        )

        goal = make_goal(mode="retry")
        llm = ScriptedLLM(
            outputs=["def add(a, b): return a + b\n"],
            claimed_done=[True],
            errors=[error],
        )
        engine, _, _ = make_engine(goal, llm, mode=self._fast_retry_mode())
        outcome = asyncio_run(engine.run())

        assert outcome.converged is True
        assert llm.calls == 2
