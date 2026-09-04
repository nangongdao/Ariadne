"""M3 闭环验收基准 —— 验收项 4 / 5 / 6。

跑 `loop_cases.ALL_CASES` 里的每个缺陷用例：产出物**真落盘**、pytest
**真执行**、收敛判定走真实 Verifier。这是 COMMAND 断言第一次进入闭环
验收 —— `test_loop_engine.py::TestEndToEndConvergence` 只能用 REGEX 断言
代理，因为在本次修复之前，模型输出根本不会写进工作目录（`artifact.py`
的模块 docstring 记了那次实测）。

## 三个指标的可信度不同，别混着读

| 验收项 | 指标 | 性质 |
|---|---|---|
| 4 | 假完成拦截率 ≥ 95% | **真验收**。被测对象是 Ralph 机制，与模型智能无关 |
| 5 | 闭环达标率 ≥ 85% | **机制验收**。模型是脚本化的 |
| 6 | 平均 ≤ 3 轮 | 同上 |

验收项 5/6 要成为**能力验收**，需把 `ScriptedRepairModel` 换成真实
provider 适配器（`runtime_module.llm`）并接一个未经调参的缺陷集 ——
本机没有 provider key，那部分留空。不把脚本模型的数字说成 LLM 能力，
是因为这个仓库已经吃过三次"读数测的不是它声称的东西"的亏。

用例集的分布是人为设计的（17 可修复 / 3 不可修复），所以达标率的分子
分母都由用例集决定：它证明的是"闭环在这些行为模式下判定正确"，不是
"闭环能修好 85% 的真实缺陷"。

## 拦截率的真值从哪来

关键设计：**不能拿 Verifier 的判定同时当分子和分母** —— 那样"拦截率"
恒等于 100%，是个永远不会红的装饰。这里的真值由**用例集声明**：
`expected_iterations=N` 意味着第 1..N-1 轮的实现是错的，`converges=False`
意味着所有轮次都是错的。假完成样本 = 那些轮次里模型自称完成的；拦截
成功 = Loop 在那一轮确实没判收敛。这样只要 Verifier 把失败当成功（哪怕
一轮），拦截率就会掉下去。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest

from ariadne.loop_module.budget import BudgetGuard, InMemoryCounter
from ariadne.loop_module.checkpoint import InMemoryCheckpointStore
from ariadne.loop_module.engine import LLMResponse, LoopConfig, LoopEngine
from ariadne.loop_module.goal import Assertion, AssertionKind, Budget, Goal
from ariadne.loop_module.state_machine import LoopState
from loop_cases import (
    ALL_CASES,
    NEVER_CONVERGES,
    REPAIRABLE,
    RepairCase,
    fenced,
)

# 单个用例的轮次上限。设 5 而非 3：达标率要允许"超过 3 轮才收敛"被记为
# 达标但拉高平均轮次；卡在 3 会把两个指标纠缠在一起。
MAX_ITERATIONS = 5


@dataclass
class ScriptedRepairModel:
    """按用例脚本逐轮给出实现。

    **每轮都 claimed_done=True** —— 真实 LLM 确实爱说"我修好了"，而这正是
    Ralph 机制要处理的输入。
    """

    case: RepairCase
    calls: int = 0
    # 每轮实际发出的实现，供真值比对
    served: list[str] = field(default_factory=list)

    async def complete(self, prompt: str, *, model: str) -> LLMResponse:
        idx = self.calls
        self.calls += 1
        attempts = self.case.attempts
        source = attempts[idx] if idx < len(attempts) else attempts[-1]
        self.served.append(source)
        return LLMResponse(
            output=fenced(source, self.case.filename),
            input_tokens=120,
            output_tokens=180,
            claimed_done=True,
            model=model,
            cost_usd=Decimal("0.002"),
        )


@dataclass(frozen=True)
class CaseResult:
    case: RepairCase
    converged: bool
    iterations: int
    final_state: LoopState
    workdir: Path
    # 该轮实现按**用例声明的真值**是否正确
    truth_correct_by_iteration: tuple[bool, ...]
    # 该轮 Loop 是否判了收敛
    loop_converged_by_iteration: tuple[bool, ...]
    # 该轮模型是否自称完成
    claimed_by_iteration: tuple[bool, ...]

    @property
    def false_completions(self) -> int:
        """自称完成但真值为错的轮次数。"""
        return sum(
            1
            for claimed, correct in zip(
                self.claimed_by_iteration,
                self.truth_correct_by_iteration,
                strict=True,
            )
            if claimed and not correct
        )

    @property
    def false_completions_caught(self) -> int:
        """上述轮次里 Loop 没判收敛的（即拦截成功）。"""
        return sum(
            1
            for claimed, correct, converged in zip(
                self.claimed_by_iteration,
                self.truth_correct_by_iteration,
                self.loop_converged_by_iteration,
                strict=True,
            )
            if claimed and not correct and not converged
        )


def _truth_for_iteration(case: RepairCase, iteration: int) -> bool:
    """用例声明的真值：第 `iteration` 轮（1-based）的实现对不对。

    真值来自用例集而非 Verifier —— 这是拦截率能真正失败的前提。
    """
    if not case.converges:
        return False
    return iteration >= case.expected_iterations


def _build_goal(case: RepairCase) -> Goal:
    return Goal(
        task=f"修复 {case.filename} 使 pytest 全部通过（缺陷类型：{case.category}）",
        assertions=(
            Assertion(
                id="tests_pass",
                kind=AssertionKind.COMMAND,
                spec={"cmd": "python -m pytest -q"},
                hint="读 pytest 的失败输出，定位断言失败的具体行为再改",
            ),
        ),
        budget=Budget(
            max_iterations=MAX_ITERATIONS,
            max_total_tokens=1_000_000,
            max_cost_usd=100.0,
            max_wall_clock_seconds=900,
        ),
        mode="verify_execute",
        # 放宽"得分无进展"的容忍度，让可修复用例有完整的多轮修正空间。
        # 注意这**不会**关掉振荡熔断：`stall_after`（连续 3 轮同一失败签名）
        # 是另一条路径，不可修复用例仍会落到 STALLED —— 那正是期望结果，
        # 比跑满预算更省钱且给了用户明确诊断（见 fingerprint 模块 docstring）。
        stall_patience=99,
    )


def run_case(case: RepairCase, tmp_root: Path) -> CaseResult:
    """跑一个用例到终态。真落盘、真执行 pytest。"""
    workdir = tmp_root / case.name
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / case.filename).write_text(case.buggy, encoding="utf-8")
    (workdir / "test_solution.py").write_text(case.tests, encoding="utf-8")

    goal = _build_goal(case)
    loop_id = f"bench-{case.name}"
    store = InMemoryCheckpointStore()
    model = ScriptedRepairModel(case)

    engine = LoopEngine(
        LoopConfig(
            goal=goal,
            loop_id=loop_id,
            project_id=uuid.uuid4(),
            budget_guard=BudgetGuard(loop_id, goal.budget, InMemoryCounter()),
            llm=model,
            checkpoint_store=store,
            artifact_path=workdir,
        )
    )
    outcome = asyncio.run(engine.run())

    checkpoints = store.all(loop_id)
    claimed: list[bool] = []
    loop_converged: list[bool] = []
    truth: list[bool] = []
    for checkpoint in checkpoints:
        verdict = checkpoint.verdict
        claimed.append(bool(verdict and verdict.claimed_done))
        loop_converged.append(bool(verdict and verdict.converged))
        truth.append(_truth_for_iteration(case, checkpoint.iteration))

    return CaseResult(
        case=case,
        converged=outcome.converged,
        iterations=outcome.iterations,
        final_state=outcome.final_state,
        workdir=workdir,
        truth_correct_by_iteration=tuple(truth),
        loop_converged_by_iteration=tuple(loop_converged),
        claimed_by_iteration=tuple(claimed),
    )


@pytest.fixture(scope="module")
def results(tmp_path_factory: pytest.TempPathFactory) -> list[CaseResult]:
    """跑一次全量用例，多条断言复用 —— 每轮都真起 pytest 子进程，重跑很贵。"""
    root = tmp_path_factory.mktemp("loop_bench")
    return [run_case(case, root) for case in ALL_CASES]


class TestFalseCompletionInterception:
    """验收项 4：假完成拦截率 ≥ 95%。三个指标里唯一的真验收。"""

    def test_interception_rate_meets_threshold(
        self, results: list[CaseResult]
    ) -> None:
        total = sum(r.false_completions for r in results)
        caught = sum(r.false_completions_caught for r in results)
        assert total > 0, "用例集没制造出任何假完成样本，指标无意义"
        rate = caught / total
        leaked = [
            r.case.name
            for r in results
            if r.false_completions != r.false_completions_caught
        ]
        assert rate >= 0.95, (
            f"假完成拦截率 {rate:.1%}（{caught}/{total}）低于 95% 门槛；"
            f"漏判用例: {leaked}"
        )

    def test_never_converging_cases_do_not_report_success(
        self, results: list[CaseResult]
    ) -> None:
        """模型每轮都自称完成，但实现从未正确 —— 绝不能判成功。

        闭环最危险的失败模式不是"修不好"，是"没修好却说修好了"。
        """
        names = {c.name for c in NEVER_CONVERGES}
        for result in results:
            if result.case.name in names:
                assert not result.converged, (
                    f"{result.case.name} 被判成收敛，但它的实现从未通过测试"
                )
                assert result.final_state is not LoopState.CONVERGED

    def test_claimed_done_does_not_short_circuit_verification(
        self, results: list[CaseResult]
    ) -> None:
        """首轮实现错误的用例不能在首轮收敛 —— 否则 claimed_done 短路了验证。"""
        for result in results:
            if result.case.converges and result.case.expected_iterations > 1:
                assert result.iterations > 1, (
                    f"{result.case.name} 在首轮就判收敛，但首轮实现是错的"
                )


class TestClosedLoopSuccessRate:
    """验收项 5/6：闭环达标率 ≥ 85%、平均 ≤ 3 轮。机制验收，非 LLM 能力验收。"""

    def test_success_rate_meets_threshold(self, results: list[CaseResult]) -> None:
        repairable = {c.name for c in REPAIRABLE}
        subset = [r for r in results if r.case.name in repairable]
        converged = [r for r in subset if r.converged]
        rate = len(converged) / len(subset)
        failed = [r.case.name for r in subset if not r.converged]
        assert rate >= 0.85, (
            f"闭环达标率 {rate:.1%}（{len(converged)}/{len(subset)}）低于 85%；"
            f"未收敛用例: {failed}"
        )

    def test_average_iterations_within_budget(
        self, results: list[CaseResult]
    ) -> None:
        repairable = {c.name for c in REPAIRABLE}
        converged = [r for r in results if r.case.name in repairable and r.converged]
        assert converged, "没有任何用例收敛，平均轮次无意义"
        average = sum(r.iterations for r in converged) / len(converged)
        assert average <= 3.0, f"平均收敛轮次 {average:.2f} 超过 3 轮"

    def test_converges_at_expected_iteration(
        self, results: list[CaseResult]
    ) -> None:
        """在模型给出正确实现的那一轮收敛，不早不晚。

        早了说明验证被短路，晚了说明落盘或反馈有延迟 —— 两种都是闭环
        缺陷，只看总达标率会漏掉。
        """
        for result in results:
            if not result.case.converges or not result.case.expected_iterations:
                continue
            assert result.iterations == result.case.expected_iterations, (
                f"{result.case.name} 在第 {result.iterations} 轮收敛，"
                f"期望第 {result.case.expected_iterations} 轮"
            )


class TestArtifactsActuallyLandOnDisk:
    """闭环的前提：模型输出真的写进了工作目录。

    单独立一组是因为这正是此前缺失的一环。没有它，上面所有指标都会退化
    成"每轮跑同一份没变过的文件"，而测试仍然是绿的 —— Loop 会老老实实跑
    到 MAX_ITERATIONS 报不收敛，看起来只是"模型不够聪明"。
    """

    def test_disk_holds_last_served_implementation(
        self, results: list[CaseResult]
    ) -> None:
        """磁盘内容必须等于模型最后一轮给出的实现。"""
        for result in results:
            target = result.workdir / result.case.filename
            assert target.is_file(), f"{result.case.name} 的产出物不存在"
            on_disk = target.read_text(encoding="utf-8")
            expected = result.case.attempts[
                min(result.iterations, len(result.case.attempts)) - 1
            ]
            assert on_disk == expected, (
                f"{result.case.name} 磁盘内容与模型最后一轮输出不一致 —— "
                f"落盘没生效或写错了轮次"
            )

    def test_converged_cases_no_longer_hold_buggy_source(
        self, results: list[CaseResult]
    ) -> None:
        """收敛的用例，磁盘上不能还是最初那份缺陷实现。"""
        for result in results:
            if not result.converged:
                continue
            on_disk = (result.workdir / result.case.filename).read_text(
                encoding="utf-8"
            )
            assert on_disk != result.case.buggy, (
                f"{result.case.name} 判了收敛，但磁盘上仍是缺陷版本"
            )

    def test_multi_iteration_cases_show_progress(
        self, results: list[CaseResult]
    ) -> None:
        """多轮用例必须真的跑了多轮 —— 否则说明反馈没驱动到下一轮。"""
        for result in results:
            if result.case.converges and result.case.expected_iterations >= 2:
                assert result.iterations >= 2, (
                    f"{result.case.name} 只跑了 {result.iterations} 轮"
                )


def test_benchmark_report(results: list[CaseResult]) -> None:
    """把三个指标打出来，便于在 CI 日志里直接读到数字。"""
    repairable = {c.name for c in REPAIRABLE}
    subset = [r for r in results if r.case.name in repairable]
    converged = [r for r in subset if r.converged]
    total_fc = sum(r.false_completions for r in results)
    caught_fc = sum(r.false_completions_caught for r in results)
    average = (
        sum(r.iterations for r in converged) / len(converged) if converged else 0.0
    )

    print("\n===== M3 闭环验收基准 =====")
    print(f"用例总数          : {len(results)}（可修复 {len(subset)}）")
    print(
        f"验收项 5 达标率   : {len(converged)}/{len(subset)} = "
        f"{len(converged) / len(subset):.1%}（门槛 85%）"
    )
    print(f"验收项 6 平均轮次 : {average:.2f}（门槛 ≤ 3）")
    if total_fc:
        print(
            f"验收项 4 拦截率   : {caught_fc}/{total_fc} = "
            f"{caught_fc / total_fc:.1%}（门槛 95%）"
        )
    print("--- 逐用例 ---")
    for r in results:
        mark = "OK " if r.converged == r.case.converges else "!! "
        print(
            f"{mark}{r.case.name:<28} {r.final_state.value:<16} "
            f"{r.iterations} 轮  假完成 {r.false_completions}"
        )
