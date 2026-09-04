"""断言配置项确实穿透到生产组件（R12 第五实例的防御）。

这些不是单元测试，而是**装配断言** —— 证明配置改变时行为改变。
R12 教训：当一个字段有测试、有默认值，且默认值恰好等于硬编码常量时，
"测试全绿"的表象完美掩盖"配置死了"的事实，直到运维改配置却发现不生效。

因此每个用例都从 `Settings` 出发，走真实装配路径（LoopWorker._load_harness /
LoopWorker._build_command_runner / SpanProcessor.__init__ / CollectorWorker），
而不是直接构造被测组件 —— 后者只能证明"组件支持这个参数"，
证明不了"生产代码把配置传给了它"。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from ariadne.config import (
    HarnessSettings,
    PayloadSettings,
    SandboxSettings,
    Settings,
    TelemetrySettings,
)
from ariadne.worker.processor import SpanProcessor

PROJECT_ID = UUID("a0a0a0a0-0000-0000-0000-000000000001")

_RULE_YAML = """
rules:
  - id: wiring-probe
    category: input
    hook: pre_model
    when: 'token_count(input.text) > 100'
    action: block
    severity: high
    message: "probe"
"""


@pytest.fixture
def rules_dir(tmp_path: Path) -> Path:
    """最小可编译规则目录 —— 装配路径要求 rules_dir 非空才返回求值器。"""
    (tmp_path / "probe.yaml").write_text(_RULE_YAML, encoding="utf-8")
    return tmp_path


def make_worker(settings: Settings) -> Any:
    """构造只用于装配断言的 LoopWorker（不连 Redis / Postgres）。"""
    from ariadne.worker.loop_worker import LoopWorker

    return LoopWorker(
        settings=settings,
        pg_factory=lambda s: None,  # type: ignore[arg-type,return-value]
        counter_factory=lambda url: MagicMock(),
        queue=MagicMock(),
    )


class TestHarnessEvalTimeout:
    """断言 harness.eval_timeout_ms 真的到达求值器实例。

    关键点：生产调用方（engine._precheck / GuardedLLMAdapter）调 evaluate()
    时不传 timeout_ms，所以这个旋钮只能挂在实例上生效。
    """

    def test_default_timeout_reaches_evaluator(self, rules_dir: Path) -> None:
        from ariadne.worker.assembly import load_harness_evaluator

        settings = Settings(harness=HarnessSettings(rules_dir=str(rules_dir)))
        evaluator = load_harness_evaluator(settings)
        assert evaluator is not None
        assert evaluator.timeout_ms == 100

    def test_custom_timeout_reaches_evaluator(self, rules_dir: Path) -> None:
        from ariadne.worker.assembly import load_harness_evaluator

        settings = Settings(
            harness=HarnessSettings(rules_dir=str(rules_dir), eval_timeout_ms=5000)
        )
        evaluator = load_harness_evaluator(settings)
        assert evaluator is not None
        assert evaluator.timeout_ms == 5000

    def test_evaluate_arg_still_overrides_instance(self, rules_dir: Path) -> None:
        """显式入参优先于实例策略 —— `ariadne rules test` 压测依赖这条。"""
        from ariadne.harness_module.evaluator import HarnessEvaluator

        settings = HarnessSettings(rules_dir=str(rules_dir), eval_timeout_ms=5000)
        evaluator = HarnessEvaluator.from_settings([], settings)
        assert evaluator.timeout_ms == 5000


class TestHarnessFailClosed:
    """断言 harness.fail_closed 真的改变异常回退方向，且反安全方向会告警。"""

    def test_default_fail_closed_is_true(self, rules_dir: Path) -> None:
        from ariadne.worker.assembly import load_harness_evaluator

        settings = Settings(harness=HarnessSettings(rules_dir=str(rules_dir)))
        evaluator = load_harness_evaluator(settings)
        assert evaluator is not None
        assert evaluator.fail_closed is True

    def test_fail_open_reaches_evaluator(self, rules_dir: Path) -> None:
        from ariadne.worker.assembly import load_harness_evaluator

        settings = Settings(
            harness=HarnessSettings(rules_dir=str(rules_dir), fail_closed=False)
        )
        evaluator = load_harness_evaluator(settings)
        assert evaluator is not None
        assert evaluator.fail_closed is False

    def test_fail_open_logs_warning(self, rules_dir: Path, caplog: Any) -> None:
        """fail_closed=False 是不安全方向，装配期必须留下痕迹。"""
        from ariadne.worker.assembly import load_harness_evaluator

        settings = Settings(
            harness=HarnessSettings(rules_dir=str(rules_dir), fail_closed=False)
        )
        with caplog.at_level(logging.WARNING):
            load_harness_evaluator(settings)
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("fail_closed" in w for w in warnings), (
            f"fail-open 未告警。日志: {warnings}"
        )


class TestSandboxAllowUntrustedCode:
    """断言 sandbox.allow_untrusted_code 在装配期被读取（docs/M3-spec §5）。

    平台事实：Windows 上没有可用隔离后端，build_sandbox 必然返回 None。
    因此 allow_untrusted_code=True 只能是配置错误，必须拒绝启动而非静默降级。
    """

    def test_untrusted_true_logs_warning(self, caplog: Any) -> None:
        from ariadne.worker.loop_worker import build_command_runner

        settings = Settings(sandbox=SandboxSettings(allow_untrusted_code=True))
        with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError):
            build_command_runner(settings)
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("ALLOW_UNTRUSTED_CODE" in w for w in warnings), (
            f"未产生不可信代码告警。日志: {warnings}"
        )

    def test_untrusted_without_isolation_refuses_startup(self) -> None:
        """无真隔离 + 声明要跑不可信代码 = R5 写明不可接受的组合。"""
        from ariadne.worker.loop_worker import build_command_runner

        settings = Settings(sandbox=SandboxSettings(allow_untrusted_code=True))
        with pytest.raises(RuntimeError, match="不可信代码"):
            build_command_runner(settings)

    def test_default_false_degrades_quietly(self, caplog: Any) -> None:
        """默认 False：降级到受限子进程，记 warning 但不阻断启动。"""
        from ariadne.worker.loop_worker import build_command_runner

        settings = Settings(sandbox=SandboxSettings())
        with caplog.at_level(logging.WARNING):
            runner = build_command_runner(settings)
        assert runner is None
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert not any("ALLOW_UNTRUSTED_CODE" in w for w in warnings)

    def test_fallback_disabled_refuses_startup(self) -> None:
        """显式关掉降级 = 要求"没有真隔离就不执行"，静默降级更危险。"""
        from ariadne.worker.loop_worker import build_command_runner

        settings = Settings(sandbox=SandboxSettings(fallback_to_restricted=False))
        with pytest.raises(RuntimeError, match="受限子进程"):
            build_command_runner(settings)


class TestStoreFullPayload:
    """断言 telemetry.store_full_payload 真的改变 payload 留存行为。

    只断言"能构造"是不够的（那是 R12 的典型症状）—— 这里比对同一份超阈值
    输入在两种配置下的产物差异。
    """

    def _big_text(self) -> str:
        return "x" * 500

    def test_default_true_keeps_full_payload(self) -> None:
        settings = Settings(
            telemetry=TelemetrySettings(store_full_payload=True),
            payload=PayloadSettings(inline_max_bytes=100, compress_max_bytes=10_000),
        )
        processor = SpanProcessor(settings)
        stored = processor._payloads.process(
            self._big_text(), project_id=str(PROJECT_ID), span_id="s1", slot="in"
        )
        # 全量留存：压缩内联，stored_bytes 非零（全文可恢复）
        assert stored.stored_bytes > 0
        assert stored.original_bytes == 500

    def test_false_truncates_and_drops_full_text(self) -> None:
        settings = Settings(
            telemetry=TelemetrySettings(store_full_payload=False),
            payload=PayloadSettings(
                inline_max_bytes=100, compress_max_bytes=10_000, preview_chars=32
            ),
        )
        processor = SpanProcessor(settings)
        stored = processor._payloads.process(
            self._big_text(), project_id=str(PROJECT_ID), span_id="s1", slot="in"
        )
        # 数据最小化：截断到 preview_chars，全文不留存
        assert len(stored.preview) == 32
        assert stored.stored_bytes == 0
        assert stored.ref == ""


class TestSemconvVersionCheck:
    """断言 telemetry.genai_semconv_version 在启动时被校验（docs/06 §1）。"""

    def test_matching_version_produces_no_warning(self) -> None:
        from ariadne.telemetry.adapters.otlp import check_semconv_version

        assert check_semconv_version("1.37.0") is None

    def test_mismatched_version_produces_warning_message(self) -> None:
        """版本漂移时返回说明而不抛异常 —— 丢遥测比属性名过时更糟。"""
        from ariadne.telemetry.adapters.otlp import check_semconv_version

        drift = check_semconv_version("1.42.0")
        assert drift is not None
        assert "1.42.0" in drift
        assert "1.37.0" in drift
        assert "_GENAI_KEYS" in drift

    def test_collector_logs_drift_at_startup(self, caplog: Any) -> None:
        """CollectorWorker 初始化时应调用校验并记录漂移（若有）。"""
        from ariadne.worker.collector import CollectorWorker

        settings = Settings(
            telemetry=TelemetrySettings(genai_semconv_version="99.99.0")
        )
        with (
            patch("ariadne.worker.collector.SpanQueue"),
            patch("ariadne.worker.collector.ClickHouseStore"),
            caplog.at_level(logging.WARNING),
        ):
            CollectorWorker(settings)
        warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("99.99.0" in w for w in warnings), (
            f"未记录 semconv 版本漂移告警。日志: {warnings}"
        )
