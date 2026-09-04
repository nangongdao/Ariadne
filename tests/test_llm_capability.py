"""真实 LLM 能力验收（需要 provider key）。

## 目的

验证真实 LLM（Anthropic Claude / OpenAI GPT-4）修复代码缺陷的能力。
与 `test_loop_benchmark.py` 的 M3 机制验收不同：
- M3 使用脚本化模型，验证闭环机制正确性
- 本测试使用真实 LLM，验证实际修复能力

## 跳过条件

- 未设置 ANTHROPIC_API_KEY 或 OPENAI_API_KEY → pytest.skip
- 未设置 RUN_LLM_CAPABILITY_TESTS=1 → pytest.skip（防止 CI 意外运行）

## 运行方式

```bash
# 1. 配置 provider key
export ANTHROPIC_API_KEY=sk-ant-...
export RUN_LLM_CAPABILITY_TESTS=1

# 2. 运行全部验收（约 $50-100）
pytest tests/test_llm_capability.py -v --tb=short

# 3. 仅跑简单用例（约 $10）
pytest tests/test_llm_capability.py::test_anthropic_capability_easy -v

# 4. 查看报告
cat capability_report_*.json
```

## 成本控制

```bash
# 设置总预算（默认 $100）
export LLM_EVAL_BUDGET_USD=50

# 单个用例预算（代码中配置）
MAX_ITERATIONS = 10
MAX_COST_PER_CASE = 2.0  # $2
```

## 输出

生成 JSON 报告：
- 达标率（收敛的用例比例）
- 平均轮次
- 平均成本
- 假完成拦截率
- 按难度分组的统计
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from ariadne.loop_module.budget import BudgetGuard, InMemoryCounter
from ariadne.loop_module.checkpoint import InMemoryCheckpointStore
from ariadne.loop_module.engine import LLMResponse, LoopConfig, LoopEngine
from ariadne.loop_module.goal import Assertion, AssertionKind, Budget, Goal
from ariadne.loop_module.state_machine import LoopState
from capability_cases import ALL_CASES, BY_DIFFICULTY, CapabilityCase

# 单个用例预算
MAX_ITERATIONS = 10
MAX_COST_PER_CASE_USD = 2.0
MAX_TOKENS_PER_CASE = 100_000
MAX_WALL_CLOCK_SECONDS = 600

# 全局预算（可通过环境变量覆盖）
DEFAULT_TOTAL_BUDGET_USD = 100.0


# ========== 跳过条件 ==========


def _should_skip() -> tuple[bool, str]:
    """检查是否应该跳过测试。"""
    if os.getenv("RUN_LLM_CAPABILITY_TESTS") != "1":
        return True, "需要设置 RUN_LLM_CAPABILITY_TESTS=1"

    has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY"))
    has_openai = bool(os.getenv("OPENAI_API_KEY"))

    if not has_anthropic and not has_openai:
        return True, "需要设置 ANTHROPIC_API_KEY 或 OPENAI_API_KEY"

    return False, ""


# ========== 真实 LLM 适配器包装 ==========


@dataclass
class RealLLMAdapter:
    """真实 LLM 适配器（封装 runtime_module.llm）。

    记录每次调用，用于统计成本和 token 使用。
    """

    provider: str  # "anthropic" or "openai"
    model: str
    calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: Decimal = field(default_factory=lambda: Decimal("0"))

    async def complete(self, prompt: str, *, model: str) -> LLMResponse:
        """调用真实 LLM。"""
        self.calls += 1

        # 根据 provider 选择适配器
        from pydantic import SecretStr

        from ariadne.config import LlmSettings
        from ariadne.loop_module.engine import LLMClient

        client: LLMClient
        if self.provider == "anthropic":
            from ariadne.runtime_module.llm.anthropic import AnthropicLLMClient

            settings = LlmSettings(
                provider="anthropic",
                api_key=SecretStr(os.environ["ANTHROPIC_API_KEY"]),
                model=self.model,
                base_url="https://api.anthropic.com",
            )
            client = AnthropicLLMClient(settings)
        elif self.provider == "openai":
            from ariadne.runtime_module.llm.openai import OpenAILLMClient

            settings = LlmSettings(
                provider="openai",
                api_key=SecretStr(os.environ["OPENAI_API_KEY"]),
                model=self.model,
                base_url="https://api.openai.com/v1",
            )
            client = OpenAILLMClient(settings)
        else:
            msg = f"未知 provider: {self.provider}"
            raise ValueError(msg)

        # 调用真实 LLM
        response = await client.complete(prompt, model=self.model)

        # 累计统计
        self.total_input_tokens += response.input_tokens
        self.total_output_tokens += response.output_tokens
        self.total_cost_usd += response.cost_usd

        return response


# ========== 用例执行器 ==========


@dataclass(frozen=True)
class CaseResult:
    """单个用例的执行结果。"""

    case: CapabilityCase
    converged: bool
    iterations: int
    final_state: LoopState
    cost_usd: Decimal
    input_tokens: int
    output_tokens: int
    workdir: Path
    error: str = ""


async def _run_case(
    case: CapabilityCase,
    adapter: RealLLMAdapter,
    workdir: Path,
) -> CaseResult:
    """运行单个用例。"""
    # 准备工作目录
    case_dir = workdir / case.name
    case_dir.mkdir(parents=True, exist_ok=True)

    # 写入缺陷代码
    (case_dir / case.filename).write_text(case.buggy, encoding="utf-8")

    # 写入测试
    (case_dir / "test_solution.py").write_text(case.tests, encoding="utf-8")

    # 构造 Goal
    goal = Goal(
        task=f"修复 {case.filename} 使 pytest 全部通过（缺陷类型：{case.category}）",
        assertions=(
            Assertion(
                id="tests_pass",
                kind=AssertionKind.COMMAND,
                spec={
                    "cmd": "pytest test_solution.py -xvs",
                    "exit_code": 0,
                },
                blocking=True,
                hint="运行 pytest，所有测试通过",
            ),
        ),
        budget=Budget(
            max_iterations=MAX_ITERATIONS,
            max_total_tokens=MAX_TOKENS_PER_CASE,
            max_cost_usd=float(MAX_COST_PER_CASE_USD),
            max_wall_clock_seconds=MAX_WALL_CLOCK_SECONDS,
        ),
    )

    # 构造 LoopConfig
    from ariadne.loop_module.modes import LoopModeFactory

    mode = LoopModeFactory("quality")
    counter = InMemoryCounter()
    cfg = LoopConfig(
        goal=goal,
        loop_id=case.name,
        project_id=uuid.uuid4(),
        budget_guard=BudgetGuard(case.name, goal.budget, counter),
        llm=adapter,
        checkpoint_store=InMemoryCheckpointStore(),
        mode=mode,
        artifact_path=case_dir,
    )

    # 运行 Loop
    engine = LoopEngine(cfg)

    try:
        before_input = adapter.total_input_tokens
        before_output = adapter.total_output_tokens
        before_cost = adapter.total_cost_usd

        outcome = await engine.run()

        after_input = adapter.total_input_tokens
        after_output = adapter.total_output_tokens
        after_cost = adapter.total_cost_usd

        return CaseResult(
            case=case,
            converged=outcome.converged,
            iterations=outcome.iterations,
            final_state=outcome.final_state,
            cost_usd=after_cost - before_cost,
            input_tokens=after_input - before_input,
            output_tokens=after_output - before_output,
            workdir=case_dir,
        )
    except Exception as e:
        return CaseResult(
            case=case,
            converged=False,
            iterations=outcome.iterations if 'outcome' in locals() else 0,
            final_state=LoopState.REJECTED,
            cost_usd=Decimal("0"),
            input_tokens=0,
            output_tokens=0,
            workdir=case_dir,
            error=str(e),
        )


# ========== 报告生成 ==========


def _generate_report(
    provider: str,
    model: str,
    results: list[CaseResult],
    total_cost_usd: Decimal,
) -> dict[str, Any]:
    """生成验收报告。"""
    total = len(results)
    converged = sum(1 for r in results if r.converged)
    timeout = sum(1 for r in results if r.final_state == LoopState.BUDGET_EXCEEDED)

    # 达标用例的统计
    converged_results = [r for r in results if r.converged]
    avg_iterations = (
        sum(r.iterations for r in converged_results) / len(converged_results)
        if converged_results
        else 0.0
    )
    avg_cost = (
        sum(r.cost_usd for r in converged_results) / len(converged_results)
        if converged_results
        else Decimal("0")
    )

    # 按难度分组
    by_difficulty: dict[str, dict[str, Any]] = {}
    for difficulty in ["easy", "medium", "hard"]:
        subset = [r for r in results if r.case.difficulty == difficulty]
        if not subset:
            continue
        subset_converged = sum(1 for r in subset if r.converged)
        by_difficulty[difficulty] = {
            "total": len(subset),
            "converged": subset_converged,
            "rate": subset_converged / len(subset),
            "avg_iterations": (
                sum(r.iterations for r in subset if r.converged) / subset_converged
                if subset_converged > 0
                else 0.0
            ),
        }

    return {
        "provider": provider,
        "model": model,
        "timestamp": datetime.now(UTC).isoformat(),
        "total_cases": total,
        "converged": converged,
        "convergence_rate": converged / total if total > 0 else 0.0,
        "avg_iterations": float(avg_iterations),
        "avg_cost_usd": float(avg_cost),
        "total_cost_usd": float(total_cost_usd),
        "timeout_cases": timeout,
        "by_difficulty": by_difficulty,
        "details": [
            {
                "name": r.case.name,
                "category": r.case.category,
                "difficulty": r.case.difficulty,
                "converged": r.converged,
                "iterations": r.iterations,
                "final_state": r.final_state.name,
                "cost_usd": float(r.cost_usd),
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "error": r.error,
            }
            for r in results
        ],
    }


def _save_report(report: dict[str, Any], workdir: Path) -> Path:
    """保存报告到 JSON 文件。"""
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    filename = f"capability_report_{report['provider']}_{timestamp}.json"
    path = workdir / filename
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# ========== 测试用例 ==========


async def _run_capability_suite(
    provider: str,
    model: str,
    cases: list[CapabilityCase],
    workdir: Path,
) -> dict[str, Any]:
    """运行能力验收套件。"""
    adapter = RealLLMAdapter(provider=provider, model=model)
    results: list[CaseResult] = []

    total_budget = Decimal(
        str(os.getenv("LLM_EVAL_BUDGET_USD", str(DEFAULT_TOTAL_BUDGET_USD)))
    )

    for case in cases:
        # 检查预算
        if adapter.total_cost_usd >= total_budget:
            print(f"⚠️  达到总预算上限 ${total_budget}，停止执行")
            break

        print(f"\n{'='*60}")
        print(f"🧪 [{case.difficulty.upper()}] {case.name} ({case.category})")
        print(f"{'='*60}")

        result = await _run_case(case, adapter, workdir)
        results.append(result)

        status = "✅ 收敛" if result.converged else "❌ 未收敛"
        print(
            f"{status} | {result.iterations} 轮 | "
            f"${float(result.cost_usd):.4f} | "
            f"{result.final_state.name}"
        )

    # 生成报告
    report = _generate_report(provider, model, results, adapter.total_cost_usd)

    print(f"\n{'='*60}")
    print("📊 验收报告")
    print(f"{'='*60}")
    print(f"Provider: {provider} ({model})")
    print(f"总用例: {report['total_cases']}")
    print(f"收敛: {report['converged']} ({report['convergence_rate']:.1%})")
    print(f"平均轮次: {report['avg_iterations']:.2f}")
    print(f"平均成本: ${report['avg_cost_usd']:.4f}")
    print(f"总成本: ${report['total_cost_usd']:.4f}")
    print(f"超时: {report['timeout_cases']}")
    print()
    print("按难度分组:")
    for difficulty, stats in report["by_difficulty"].items():
        print(
            f"  {difficulty.upper()}: "
            f"{stats['converged']}/{stats['total']} ({stats['rate']:.1%}), "
            f"平均 {stats['avg_iterations']:.2f} 轮"
        )

    # 保存报告
    report_path = _save_report(report, workdir)
    print(f"\n📄 报告已保存: {report_path}")

    return report


@pytest.mark.asyncio
async def test_anthropic_capability(tmp_path: Path) -> None:
    """Anthropic Claude 能力验收（全部用例）。"""
    should_skip, reason = _should_skip()
    if should_skip:
        pytest.skip(reason)

    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("需要设置 ANTHROPIC_API_KEY")

    report = await _run_capability_suite(
        provider="anthropic",
        model="claude-sonnet-4",
        cases=list(ALL_CASES),
        workdir=tmp_path,
    )

    # 验收标准（仅供参考，不阻塞测试）
    print("\n📋 验收标准（参考）:")
    print(f"  达标率: {report['convergence_rate']:.1%} (目标 ≥ 70%)")
    print(f"  平均轮次: {report['avg_iterations']:.2f} (目标 ≤ 5)")


@pytest.mark.asyncio
async def test_anthropic_capability_easy(tmp_path: Path) -> None:
    """Anthropic Claude 能力验收（仅简单用例）。"""
    should_skip, reason = _should_skip()
    if should_skip:
        pytest.skip(reason)

    if not os.getenv("ANTHROPIC_API_KEY"):
        pytest.skip("需要设置 ANTHROPIC_API_KEY")

    report = await _run_capability_suite(
        provider="anthropic",
        model="claude-sonnet-4",
        cases=list(BY_DIFFICULTY["easy"]),
        workdir=tmp_path,
    )

    # 简单用例应该有更高的达标率
    assert report["convergence_rate"] >= 0.8, "简单用例达标率应 ≥ 80%"


@pytest.mark.asyncio
async def test_openai_capability(tmp_path: Path) -> None:
    """OpenAI GPT-4 能力验收（全部用例）。"""
    should_skip, reason = _should_skip()
    if should_skip:
        pytest.skip(reason)

    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("需要设置 OPENAI_API_KEY")

    report = await _run_capability_suite(
        provider="openai",
        model="gpt-4o",
        cases=list(ALL_CASES),
        workdir=tmp_path,
    )

    print("\n📋 验收标准（参考）:")
    print(f"  达标率: {report['convergence_rate']:.1%} (目标 ≥ 70%)")
    print(f"  平均轮次: {report['avg_iterations']:.2f} (目标 ≤ 5)")


@pytest.mark.asyncio
async def test_openai_capability_easy(tmp_path: Path) -> None:
    """OpenAI GPT-4 能力验收（仅简单用例）。"""
    should_skip, reason = _should_skip()
    if should_skip:
        pytest.skip(reason)

    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("需要设置 OPENAI_API_KEY")

    report = await _run_capability_suite(
        provider="openai",
        model="gpt-4o",
        cases=list(BY_DIFFICULTY["easy"]),
        workdir=tmp_path,
    )

    assert report["convergence_rate"] >= 0.8, "简单用例达标率应 ≥ 80%"
