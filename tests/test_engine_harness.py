"""Engine Harness 集成测试 —— _precheck 接入 Harness 规则引擎。

验证：
- harness=None → M3 行为不变（硬编码放行）
- harness=BLOCK → RULES_BLOCKED → BLOCKED 终态
- harness=APPROVAL → APPROVAL_REQUIRED → HUMAN_PENDING
- harness=WARN → RULES_PASSED（不阻断）
- 审计记录在 _precheck 时写入
- GuardedLLMAdapter 的 post_model BLOCK → EXECUTING 阶段 RULES_BLOCKED
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from ariadne.harness_module.audit import InMemoryAuditSink
from ariadne.harness_module.evaluator import HarnessEvaluator
from ariadne.harness_module.loader import compile_rule_set
from ariadne.harness_module.models import (
    Action,
    HookKind,
    Rule,
    RuleCategory,
    Severity,
)
from ariadne.loop_module.budget import BudgetGuard, InMemoryCounter
from ariadne.loop_module.checkpoint import InMemoryCheckpointStore
from ariadne.loop_module.engine import LLMResponse, LoopConfig, LoopEngine
from ariadne.loop_module.goal import Assertion, AssertionKind, Budget, Goal
from ariadne.loop_module.modes import LoopModeFactory
from ariadne.runtime_module.llm.guarded import GuardedLLMAdapter

# ---------- 辅助构建 ----------


def _make_goal() -> Goal:
    return Goal(
        task="测试任务",
        assertions=(
            Assertion(
                id="test-pass",
                kind=AssertionKind.SCHEMA,
                spec={"schema": {"type": "string"}},
                blocking=True,
            ),
        ),
        budget=Budget(max_iterations=3, max_cost_usd=1.0),
        mode="quality",
    )


class _StubLLM:
    """桩 LLM。"""

    def __init__(self, output: str = "done") -> None:
        self._output = output

    async def complete(self, prompt: str, *, model: str) -> LLMResponse:
        return LLMResponse(
            output=self._output,
            input_tokens=10,
            output_tokens=5,
            claimed_done=True,
            model=model,
            cost_usd=Decimal("0.001"),
        )


def _make_config(
    *,
    harness: HarnessEvaluator | None = None,
    audit_sink: InMemoryAuditSink | None = None,
    llm: object | None = None,
    artifact_path: Path | None = None,
) -> LoopConfig:
    goal = _make_goal()
    counter = InMemoryCounter()
    return LoopConfig(
        goal=goal,
        loop_id="test-loop",
        project_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        budget_guard=BudgetGuard("test-loop", goal.budget, counter),
        llm=llm or _StubLLM(),  # type: ignore[arg-type]
        checkpoint_store=InMemoryCheckpointStore(),
        mode=LoopModeFactory("quality"),
        harness=harness,
        audit_sink=audit_sink,
        artifact_path=artifact_path,
    )


def _make_rule(
    *,
    id: str = "test-rule",
    category: RuleCategory = RuleCategory.INPUT,
    hook: HookKind = HookKind.PRE_MODEL,
    when: str = "true",
    action: Action = Action.WARN,
    severity: Severity = Severity.MEDIUM,
) -> Rule:
    return Rule(
        id=id,
        category=category,
        hook=hook,
        when=when,
        action=action,
        severity=severity,
    )


# ---------- harness=None 回归 ----------


class TestEngineNoHarness:
    """harness=None 时行为同 M3（硬编码放行）。"""

    @pytest.mark.asyncio
    async def test_no_harness_passes(self) -> None:
        """无 harness 时 _precheck 返回 RULES_PASSED。"""
        config = _make_config(harness=None)
        engine = LoopEngine(config)
        event = await engine._precheck()
        assert event.value == "RULES_PASSED"

    @pytest.mark.asyncio
    async def test_no_harness_full_loop(self) -> None:
        """无 harness 时完整 Loop 正常运行。"""
        config = _make_config(harness=None)
        engine = LoopEngine(config)
        outcome = await engine.run()
        # 正常完成（CONVERGED 或 MAX_ITERATIONS）
        assert outcome.final_state.value in ("CONVERGED", "MAX_ITERATIONS")


# ---------- harness BLOCK ----------


class TestEngineHarnessBlock:
    """harness=BLOCK → RULES_BLOCKED → BLOCKED 终态。"""

    @pytest.mark.asyncio
    async def test_precheck_block_returns_rules_blocked(self) -> None:
        """_precheck 在 harness BLOCK 时返回 RULES_BLOCKED。"""
        rule = _make_rule(
            id="block-all",
            when="true",
            action=Action.BLOCK,
        )
        evaluator = compile_rule_set([rule])
        config = _make_config(harness=evaluator)
        engine = LoopEngine(config)
        event = await engine._precheck()
        assert event.value == "RULES_BLOCKED"

    @pytest.mark.asyncio
    async def test_block_leads_to_blocked_terminal(self) -> None:
        """harness=BLOCK → run() 返回 BLOCKED 终态。"""
        rule = _make_rule(
            id="block-all",
            when="true",
            action=Action.BLOCK,
        )
        evaluator = compile_rule_set([rule])
        config = _make_config(harness=evaluator)
        engine = LoopEngine(config)
        outcome = await engine.run()
        assert outcome.final_state.value == "BLOCKED"

    @pytest.mark.asyncio
    async def test_block_with_condition(self) -> None:
        """条件 BLOCK：只有命中条件时才阻断。"""
        rule = _make_rule(
            id="block-pii",
            when="detect_pii(input.text).size() > 0",
            action=Action.BLOCK,
        )
        evaluator = compile_rule_set([rule])
        # 无 PII → 放行
        config = _make_config(harness=evaluator)
        engine = LoopEngine(config)
        engine._last_output = "clean text without pii"
        event = await engine._precheck()
        assert event.value == "RULES_PASSED"


# ---------- harness APPROVAL ----------


class TestEngineHarnessApproval:
    """harness=REQUIRE_APPROVAL → APPROVAL_REQUIRED。"""

    @pytest.mark.asyncio
    async def test_precheck_approval_returns_approval_required(self) -> None:
        """_precheck 在 harness REQUIRE_APPROVAL 时返回 APPROVAL_REQUIRED。"""
        rule = _make_rule(
            id="approval-rule",
            when="true",
            action=Action.REQUIRE_APPROVAL,
        )
        evaluator = compile_rule_set([rule])
        config = _make_config(harness=evaluator)
        engine = LoopEngine(config)
        event = await engine._precheck()
        assert event.value == "APPROVAL_REQUIRED"


# ---------- harness WARN ----------


class TestEngineHarnessWarn:
    """harness=WARN 不阻断。"""

    @pytest.mark.asyncio
    async def test_precheck_warn_passes(self) -> None:
        """_precheck 在 harness WARN 时返回 RULES_PASSED。"""
        rule = _make_rule(
            id="warn-rule",
            when="true",
            action=Action.WARN,
        )
        evaluator = compile_rule_set([rule])
        config = _make_config(harness=evaluator)
        engine = LoopEngine(config)
        event = await engine._precheck()
        assert event.value == "RULES_PASSED"


# ---------- 审计 ----------


class TestEngineAudit:
    """_precheck 审计记录写入。"""

    @pytest.mark.asyncio
    async def test_audit_written_on_precheck(self) -> None:
        """_precheck 时审计记录写入 audit_sink。"""
        rule = _make_rule(
            id="warn-rule",
            when="true",
            action=Action.WARN,
        )
        evaluator = compile_rule_set([rule])
        sink = InMemoryAuditSink()
        config = _make_config(harness=evaluator, audit_sink=sink)
        engine = LoopEngine(config)
        await engine._precheck()
        assert sink.count == 1
        record = sink.latest()
        assert record is not None
        assert record.hook == HookKind.PRE_MODEL

    @pytest.mark.asyncio
    async def test_no_audit_sink_no_error(self) -> None:
        """无 audit_sink 时不报错。"""
        rule = _make_rule(
            id="warn-rule",
            when="true",
            action=Action.WARN,
        )
        evaluator = compile_rule_set([rule])
        config = _make_config(harness=evaluator, audit_sink=None)
        engine = LoopEngine(config)
        event = await engine._precheck()
        assert event.value == "RULES_PASSED"

    @pytest.mark.asyncio
    async def test_audit_on_block(self) -> None:
        """BLOCK 时也写审计。"""
        rule = _make_rule(
            id="block-rule",
            when="true",
            action=Action.BLOCK,
        )
        evaluator = compile_rule_set([rule])
        sink = InMemoryAuditSink()
        config = _make_config(harness=evaluator, audit_sink=sink)
        engine = LoopEngine(config)
        await engine._precheck()
        assert sink.count == 1
        assert sink.latest().action == Action.BLOCK


# ---------- GuardedLLMAdapter 接入 engine ----------


class TestEngineGuardedLLM:
    """GuardedLLMAdapter 在 engine 内的 post_model BLOCK。"""

    @pytest.mark.asyncio
    async def test_guarded_llm_block_leads_to_blocked(self) -> None:
        """GuardedLLMAdapter post_model BLOCK → EXECUTING 阶段 RULES_BLOCKED。"""
        # pre_model 放行
        pre_rule = _make_rule(
            id="pre-allow",
            when="true",
            action=Action.ALLOW,
        )
        # post_model BLOCK
        post_rule = _make_rule(
            id="post-block",
            hook=HookKind.POST_MODEL,
            category=RuleCategory.OUTPUT,
            when="true",
            action=Action.BLOCK,
        )
        evaluator = compile_rule_set([pre_rule, post_rule])
        inner = _StubLLM(output="bad output")
        guarded = GuardedLLMAdapter(inner=inner, evaluator=evaluator)
        config = _make_config(llm=guarded)
        engine = LoopEngine(config)
        outcome = await engine.run()
        # post_model BLOCK 在 EXECUTING 阶段触发 → BLOCKED 终态
        assert outcome.final_state.value == "BLOCKED"

    @pytest.mark.asyncio
    async def test_guarded_llm_no_block_runs_normally(self) -> None:
        """GuardedLLMAdapter 无 BLOCK 规则时正常完成。"""
        evaluator = HarnessEvaluator(rules=[])
        inner = _StubLLM(output="ok output")
        guarded = GuardedLLMAdapter(inner=inner, evaluator=evaluator)
        config = _make_config(llm=guarded)
        engine = LoopEngine(config)
        outcome = await engine.run()
        assert outcome.final_state.value in ("CONVERGED", "MAX_ITERATIONS")


class TestEnginePrepPersist:
    """pre_persist 卡点在 engine 内的集成行为。

    GuardedArtifactWriter 由 _build_artifact_writer 自动装配（harness 存在
    时）。pre_persist BLOCK → WriteReport.error → _persist_artifacts 返回
    EXECUTION_FAILED → 每轮都不收敛 → 最后撞 MAX_ITERATIONS。硬约束语义：
    违规内容不会落盘，Loop 不可能以违规输出收敛。

    注意 artifact_path 不可省：workdir=None 时 _persist_artifacts 提前返回
    EXECUTION_DONE，**根本不会调 write()** —— 卡点就不会被求值（实测踩过）。
    """

    @staticmethod
    def _pre_persist_rule(
        *, when: str = "true", action: Action = Action.BLOCK, id: str = "pp-block"
    ) -> Rule:
        return Rule(
            id=id,
            category=RuleCategory.OUTPUT,
            hook=HookKind.PRE_PERSIST,
            when=when,
            action=action,
            severity=Severity.HIGH,
            message="输出敏感，禁止落盘",
        )

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("tmp_path_factory")
    async def test_pre_persist_block_exhausts_iterations(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """pre_persist BLOCK（任意输出都拦）→ 每轮 EXECUTION_FAILED → 最后 MAX_ITERATIONS。"""
        workdir = tmp_path_factory.mktemp("pre_persist_block")
        evaluator = compile_rule_set(
            [
                _make_rule(id="pre-allow", when="true", action=Action.ALLOW),
                self._pre_persist_rule(),
            ]
        )
        config = _make_config(harness=evaluator, artifact_path=workdir)
        engine = LoopEngine(config)
        outcome = await engine.run()
        assert outcome.final_state.value in ("MAX_ITERATIONS", "STALLED")
        # 硬约束语义：没有一次成功落盘
        assert not (workdir / "solution.py").exists()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("tmp_path_factory")
    async def test_pre_persist_block_runs_no_iterations(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """pre_persist BLOCK（任何非空输出都拦）→ 永远不会 CONVERGED。"""
        workdir = tmp_path_factory.mktemp("pre_persist_no")
        evaluator = compile_rule_set(
            [
                _make_rule(id="pre-allow", when="true", action=Action.ALLOW),
                self._pre_persist_rule(when='output.text.size() > 0'),
            ]
        )
        config = _make_config(harness=evaluator, artifact_path=workdir)
        engine = LoopEngine(config)
        outcome = await engine.run()
        assert outcome.final_state.value != "CONVERGED"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("tmp_path_factory")
    async def test_pre_persist_warn_runs_normally(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """pre_persist WARN → 正常收敛（不阻断）。"""
        workdir = tmp_path_factory.mktemp("pre_persist_warn")
        evaluator = compile_rule_set(
            [
                _make_rule(id="pre-allow", when="true", action=Action.ALLOW),
                self._pre_persist_rule(action=Action.WARN),
            ]
        )
        config = _make_config(harness=evaluator, artifact_path=workdir)
        engine = LoopEngine(config)
        outcome = await engine.run()
        assert outcome.final_state.value in ("CONVERGED", "MAX_ITERATIONS")

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("tmp_path_factory")
    async def test_pre_persist_block_writes_audit(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """pre_persist BLOCK 走 GuardedArtifactWriter 写审计。"""
        workdir = tmp_path_factory.mktemp("pre_persist_audit")
        sink = InMemoryAuditSink()
        evaluator = compile_rule_set(
            [
                _make_rule(id="pre-allow", when="true", action=Action.ALLOW),
                self._pre_persist_rule(),
            ]
        )
        config = _make_config(harness=evaluator, audit_sink=sink, artifact_path=workdir)
        engine = LoopEngine(config)
        await engine.run()
        persist_records = [r for r in sink.records if r.hook is HookKind.PRE_PERSIST]
        assert persist_records, "pre_persist 从未写审计 —— 卡点没被求值"
        assert all(r.action is Action.BLOCK for r in persist_records)
        assert all(r.context_snapshot["output_len"] >= 0 for r in persist_records)
