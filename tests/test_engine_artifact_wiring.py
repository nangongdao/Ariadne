"""engine 装配 GuardedArtifactWriter —— 接线验证。

验证 engine._build_artifact_writer 按 harness 存在与否返回包装或裸 writer。
没有这段，GuardedArtifactWriter 的全部行为在生产里都不会发生。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from ariadne.harness_module.loader import compile_rule_set
from ariadne.harness_module.models import (
    Action,
    HookKind,
    Rule,
    RuleCategory,
    Severity,
)
from ariadne.loop_module.artifact import NullArtifactWriter
from ariadne.runtime_module.artifact.guarded import GuardedArtifactWriter

PROJECT = uuid.UUID("00000000-0000-0000-0000-0000000000f1")


def _rule(
    *,
    id: str = "t",
    when: str = "true",
    action: Action = Action.BLOCK,
    hook: HookKind = HookKind.PRE_PERSIST,
) -> Rule:
    return Rule(
        id=id,
        category=RuleCategory.OUTPUT,
        hook=hook,
        when=when,
        action=action,
        severity=Severity.CRITICAL,
        message="命中",
    )


def _config(*, harness: object, artifact_writer: object = None) -> object:
    from ariadne.loop_module.budget import BudgetGuard, InMemoryCounter
    from ariadne.loop_module.checkpoint import InMemoryCheckpointStore
    from ariadne.loop_module.engine import LoopConfig
    from ariadne.loop_module.goal import Assertion, AssertionKind, Budget, Goal

    goal = Goal(
        task="生成代码",
        assertions=(
            Assertion(
                id="cmd",
                kind=AssertionKind.COMMAND,
                spec={"cmd": "pytest"},
                blocking=True,
            ),
        ),
        budget=Budget(max_iterations=2, max_cost_usd=1.0),
        mode="quality",
    )
    return LoopConfig(
        goal=goal,
        loop_id="loop-w",
        project_id=PROJECT,
        budget_guard=BudgetGuard("loop-w", goal.budget, InMemoryCounter()),
        llm=None,  # type: ignore[arg-type]
        checkpoint_store=InMemoryCheckpointStore(),
        harness=harness,  # type: ignore[arg-type]
        artifact_writer=artifact_writer,  # type: ignore[arg-type]
        artifact_path=Path("/tmp"),  # 让 engine 认为需要落盘
    )


def test_harness_wraps_artifact_writer() -> None:
    from ariadne.loop_module.engine import LoopEngine

    engine = LoopEngine(_config(harness=compile_rule_set([_rule()])))  # type: ignore[arg-type]
    assert isinstance(engine._artifact_writer, GuardedArtifactWriter)


def test_no_harness_leaves_writer_unwrapped() -> None:
    """没有规则集的 Loop 不该多出一层求值开销。"""
    from ariadne.loop_module.engine import LoopEngine

    engine = LoopEngine(_config(harness=None))
    assert not isinstance(engine._artifact_writer, GuardedArtifactWriter)


def test_wired_writer_reads_live_loop_state() -> None:
    """loop_state_provider 绑的是 bound method，取值发生在调用时。"""
    from ariadne.loop_module.engine import LoopEngine

    engine = LoopEngine(_config(harness=compile_rule_set([_rule(when="false")])))  # type: ignore[arg-type]
    writer = engine._artifact_writer
    assert isinstance(writer, GuardedArtifactWriter)
    assert writer.loop_state_provider is not None
    engine._iteration = 7
    assert writer.loop_state_provider()["iteration"] == 7


def test_injected_writer_still_goes_through_harness_gate() -> None:
    """注入自定义 writer 时仍然过 pre_persist 卡点。"""
    from ariadne.loop_module.engine import LoopEngine

    custom_writer = NullArtifactWriter()
    engine = LoopEngine(
        _config(harness=compile_rule_set([_rule()]), artifact_writer=custom_writer)  # type: ignore[arg-type]
    )
    guarded = engine._artifact_writer
    assert isinstance(guarded, GuardedArtifactWriter)
    assert guarded.inner is custom_writer


def test_default_writer_is_fenced_code_writer_when_command_assertions_present() -> None:
    """有 COMMAND 断言时默认落盘（FencedCodeWriter），否则空转。"""
    from ariadne.loop_module.artifact import FencedCodeWriter
    from ariadne.loop_module.engine import LoopEngine

    engine = LoopEngine(_config(harness=None))
    # harness=None 不包装，能直接看到内层类型
    assert isinstance(engine._artifact_writer, FencedCodeWriter)


def test_no_artifact_path_disables_write() -> None:
    """无工作目录时用 NullArtifactWriter，不落盘。"""
    from ariadne.loop_module.budget import BudgetGuard, InMemoryCounter
    from ariadne.loop_module.checkpoint import InMemoryCheckpointStore
    from ariadne.loop_module.engine import LoopConfig, LoopEngine
    from ariadne.loop_module.goal import Assertion, AssertionKind, Budget, Goal

    goal = Goal(
        task="生成代码",
        assertions=(
            Assertion(
                id="cmd",
                kind=AssertionKind.COMMAND,
                spec={"cmd": "pytest"},
                blocking=True,
            ),
        ),
        budget=Budget(max_iterations=2, max_cost_usd=1.0),
        mode="quality",
    )
    cfg = LoopConfig(
        goal=goal,
        loop_id="loop-w",
        project_id=PROJECT,
        budget_guard=BudgetGuard("loop-w", goal.budget, InMemoryCounter()),
        llm=None,  # type: ignore[arg-type]
        checkpoint_store=InMemoryCheckpointStore(),
        harness=None,
        artifact_path=None,  # 无工作目录
    )
    engine = LoopEngine(cfg)
    assert isinstance(engine._artifact_writer, NullArtifactWriter)
