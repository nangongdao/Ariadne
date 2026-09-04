"""GuardedArtifactWriter 测试 —— pre_persist 卡点求值与审计。

验证：
- BLOCK → inner.write 未被调用、WriteReport.error 含规则 id/message
- REQUIRE_APPROVAL 降级 BLOCK
- WARN 透传 + 审计
- REWRITE/ROUTE fail-open（无安全改写语义，放行 + 留痕）
- 无规则/不命中透传
- 审计：BLOCK/ALLOW 都写 record、NullAuditSink 不写
- loop_state_provider 失败不阻断写；provider 值能到规则
- 随包规则：output.yaml 的 pre_persist 规则存在；普通输出（敏感度低）通过
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from ariadne.harness_module.audit import AuditSink, InMemoryAuditSink, NullAuditSink
from ariadne.harness_module.loader import compile_rule_set
from ariadne.harness_module.models import (
    Action,
    HookKind,
    Rule,
    RuleCategory,
    Severity,
)
from ariadne.loop_module.artifact import ArtifactWriter, WriteReport
from ariadne.runtime_module.artifact.guarded import GuardedArtifactWriter


class FakeWriter:
    """桩 writer：记录调用，返回成功。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def write(self, output: str, workdir: Path) -> WriteReport:
        self.calls.append((output, workdir))
        return WriteReport()


@pytest.fixture
def fake_writer() -> FakeWriter:
    return FakeWriter()


@pytest.fixture
def audit_sink() -> InMemoryAuditSink:
    return InMemoryAuditSink()


def _rule(
    *,
    id: str = "t",
    when: str = "true",
    action: Action = Action.BLOCK,
    severity: Severity = Severity.CRITICAL,
    message: str = "命中",
    hook: HookKind = HookKind.PRE_PERSIST,
) -> Rule:
    return Rule(
        id=id,
        category=RuleCategory.OUTPUT,
        hook=hook,
        when=when,
        action=action,
        severity=severity,
        message=message,
    )


def test_no_rules_passes_through(fake_writer: FakeWriter) -> None:
    """无规则时透传 inner.write。"""
    evaluator = compile_rule_set([])
    guarded = GuardedArtifactWriter(inner=fake_writer, evaluator=evaluator)

    report = guarded.write("content", Path("/tmp"))

    assert report.ok
    assert len(fake_writer.calls) == 1
    assert fake_writer.calls[0][0] == "content"


def test_block_prevents_write(fake_writer: FakeWriter, audit_sink: AuditSink) -> None:
    """BLOCK 规则命中 → inner.write 未被调用、error 含规则 id。"""
    rules = [
        _rule(
            id="test-block",
            when="output.text.contains('secret')",
            action=Action.BLOCK,
            message="输出含敏感词",
        )
    ]
    evaluator = compile_rule_set(rules)
    guarded = GuardedArtifactWriter(
        inner=fake_writer, evaluator=evaluator, audit_sink=audit_sink
    )

    report = guarded.write("my secret password", Path("/tmp"))

    assert not report.ok  # error 非空 → ok=False
    assert "test-block" in report.error
    assert "输出含敏感词" in report.error
    assert len(fake_writer.calls) == 0  # inner 未被调用
    assert audit_sink.count == 1
    assert audit_sink.latest().action is Action.BLOCK


def test_require_approval_acts_as_block(
    fake_writer: FakeWriter, audit_sink: AuditSink
) -> None:
    """REQUIRE_APPROVAL 在此等同 BLOCK（无挂起审批的地方）。"""
    rules = [
        _rule(
            id="test-approval",
            when="true",
            action=Action.REQUIRE_APPROVAL,
            message="需审批",
        )
    ]
    evaluator = compile_rule_set(rules)
    guarded = GuardedArtifactWriter(inner=fake_writer, evaluator=evaluator)

    report = guarded.write("content", Path("/tmp"))

    assert not report.ok
    assert "test-approval" in report.error
    assert len(fake_writer.calls) == 0


def test_warn_passes_through_and_audits(
    fake_writer: FakeWriter, audit_sink: AuditSink
) -> None:
    """WARN 规则命中 → 透传 inner.write + 写审计。"""
    rules = [
        _rule(
            id="test-warn",
            when="output.text.size() > 100",
            action=Action.WARN,
            message="输出过长",
        )
    ]
    evaluator = compile_rule_set(rules)
    guarded = GuardedArtifactWriter(
        inner=fake_writer, evaluator=evaluator, audit_sink=audit_sink
    )

    long_text = "x" * 150
    report = guarded.write(long_text, Path("/tmp"))

    assert report.ok
    assert len(fake_writer.calls) == 1
    assert audit_sink.count == 1
    assert audit_sink.latest().action is Action.WARN


def test_rewrite_action_fails_open(fake_writer: FakeWriter) -> None:
    """REWRITE 在 pre_persist 无安全改写语义，fail-open（放行 + 留痕）。"""
    rules = [
        _rule(
            id="test-rewrite",
            when="true",
            action=Action.REWRITE,
        )
    ]
    evaluator = compile_rule_set(rules)
    guarded = GuardedArtifactWriter(inner=fake_writer, evaluator=evaluator)

    report = guarded.write("content", Path("/tmp"))

    assert report.ok
    assert len(fake_writer.calls) == 1


def test_route_action_fails_open(fake_writer: FakeWriter) -> None:
    """ROUTE 在 pre_persist 无路由语义，fail-open。"""
    rules = [
        _rule(
            id="test-route",
            when="true",
            action=Action.ROUTE,
        )
    ]
    evaluator = compile_rule_set(rules)
    guarded = GuardedArtifactWriter(inner=fake_writer, evaluator=evaluator)

    report = guarded.write("content", Path("/tmp"))

    assert report.ok
    assert len(fake_writer.calls) == 1


def test_null_audit_sink_does_not_write() -> None:
    """NullAuditSink 不写审计（默认行为）。"""
    rules = [_rule(id="test-block", when="true", action=Action.BLOCK)]
    evaluator = compile_rule_set(rules)
    fake = FakeWriter()
    guarded = GuardedArtifactWriter(
        inner=fake, evaluator=evaluator, audit_sink=NullAuditSink()
    )

    guarded.write("content", Path("/tmp"))

    # 没有可观测的副作用 —— NullAuditSink.write 是 no-op


def test_loop_state_provider_reaches_rules(fake_writer: FakeWriter) -> None:
    """loop_state_provider 的值能到达规则求值。"""
    rules = [
        _rule(
            id="test-iteration",
            when="ctx.loop.iteration > 5",
            action=Action.BLOCK,
            message="迭代过多",
        )
    ]
    evaluator = compile_rule_set(rules)

    def provider() -> dict[str, object]:
        return {"iteration": 10, "max_iterations": 20}

    guarded = GuardedArtifactWriter(
        inner=fake_writer, evaluator=evaluator, loop_state_provider=provider
    )

    report = guarded.write("content", Path("/tmp"))

    assert not report.ok
    assert "test-iteration" in report.error


def test_loop_state_provider_failure_does_not_block(fake_writer: FakeWriter) -> None:
    """loop_state_provider 抛异常 → 捕获并返回空 dict，规则能正常求值。

    GuardedArtifactWriter._loop_context 捕获异常返回 {} → _normalize_loop
    补全缺省 {budget_limit: 0, ...} → 规则 ctx.loop.budget_limit > 0 判定为
    False → 不命中 → ALLOW。
    """
    rules = [
        _rule(
            id="test-budget",
            # 正确写法：ctx.loop.xxx（不是 loop.xxx）
            when="ctx.loop.budget_limit > 0",
            action=Action.BLOCK,
        )
    ]
    evaluator = compile_rule_set(rules)

    def failing_provider() -> dict[str, object]:
        raise RuntimeError("provider failed")

    guarded = GuardedArtifactWriter(
        inner=fake_writer, evaluator=evaluator, loop_state_provider=failing_provider
    )

    report = guarded.write("content", Path("/tmp"))

    # provider 失败 → 空 dict → 补全为 {budget_limit: 0, ...}
    # 规则 ctx.loop.budget_limit > 0 判定为 False → 不命中 → ALLOW
    assert report.ok
    assert len(fake_writer.calls) == 1


def test_bundled_rule_output_sensitive_high_exists() -> None:
    """随包规则 output-sensitive-high（pre_persist）存在且能求值。"""
    from pathlib import Path

    from ariadne.harness_module.loader import load_rule_set

    _SHIPPED_RULES_DIR = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "ariadne"
        / "harness_module"
        / "rules"
    )

    rules = load_rule_set(_SHIPPED_RULES_DIR)
    evaluator = compile_rule_set(rules)

    # 检查规则存在
    pre_persist_rules = [
        r for r in evaluator.rules if r.rule.hook is HookKind.PRE_PERSIST
    ]
    assert len(pre_persist_rules) > 0, "随包规则里没有 pre_persist 规则"
    rule_ids = {r.rule.id for r in pre_persist_rules}
    assert "output-sensitive-high" in rule_ids


def test_bundled_rule_normal_output_passes() -> None:
    """随包规则：普通输出（敏感度低）通过。"""
    from pathlib import Path

    from ariadne.harness_module.loader import load_rule_set

    _SHIPPED_RULES_DIR = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "ariadne"
        / "harness_module"
        / "rules"
    )

    rules = load_rule_set(_SHIPPED_RULES_DIR)
    evaluator = compile_rule_set(rules)
    fake = FakeWriter()
    guarded = GuardedArtifactWriter(inner=fake, evaluator=evaluator)

    normal_output = "def add(a, b):\n    return a + b\n"
    report = guarded.write(normal_output, Path("/tmp"))

    # sensitive_score 低于 0.7 → 不触发 output-sensitive-high
    assert report.ok
    assert len(fake.calls) == 1


def test_audit_context_snapshot_no_text_leak(audit_sink: InMemoryAuditSink) -> None:
    """审计 context_snapshot 不记录输出文本本身（防二次泄漏）。"""
    rules = [
        _rule(
            id="test-block",
            when="output.text.contains('secret')",
            action=Action.BLOCK,
        )
    ]
    evaluator = compile_rule_set(rules)
    fake = FakeWriter()
    guarded = GuardedArtifactWriter(
        inner=fake, evaluator=evaluator, audit_sink=audit_sink, loop_id="test-loop"
    )

    guarded.write("my secret password", Path("/tmp"))

    record = audit_sink.latest()
    assert record is not None
    snapshot = record.context_snapshot
    assert "hook" in snapshot
    assert "loop_id" in snapshot
    assert "output_len" in snapshot
    # 不应含输出文本
    assert "text" not in snapshot
    assert "output" not in snapshot
    assert "secret" not in str(snapshot)
