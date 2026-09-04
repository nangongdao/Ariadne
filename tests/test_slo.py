"""SLO 多窗口燃烧率引擎测试。

M6 §9 验收项 #7：错误预算告警带归因。
"""

from __future__ import annotations

import pytest

from ariadne.observability.slo import (
    ALL_SLOS,
    DEFAULT_WINDOWS,
    INGEST_AVAILABILITY_SLO,
    LOOP_SUCCESS_SLO,
    BurnRateCalculator,
    SLOAlert,
    SLOLevel,
)


class TestSLOWindow:
    def test_default_windows_count(self) -> None:
        assert len(DEFAULT_WINDOWS) == 2

    def test_default_windows_levels(self) -> None:
        levels = {w.level for w in DEFAULT_WINDOWS}
        assert levels == {SLOLevel.CRITICAL, SLOLevel.WARNING}

    def test_default_windows_thresholds(self) -> None:
        critical = next(w for w in DEFAULT_WINDOWS if w.level == SLOLevel.CRITICAL)
        warning = next(w for w in DEFAULT_WINDOWS if w.level == SLOLevel.WARNING)
        assert critical.burn_rate_threshold == 14.4
        assert warning.burn_rate_threshold == 6.0

    def test_default_windows_durations(self) -> None:
        critical = next(w for w in DEFAULT_WINDOWS if w.level == SLOLevel.CRITICAL)
        warning = next(w for w in DEFAULT_WINDOWS if w.level == SLOLevel.WARNING)
        assert critical.long_window_hours == 1.0
        assert critical.short_window_minutes == 5.0
        assert warning.long_window_hours == 6.0
        assert warning.short_window_minutes == 30.0


class TestSLOSpec:
    def test_error_budget_999(self) -> None:
        assert INGEST_AVAILABILITY_SLO.error_budget == pytest.approx(0.001)

    def test_error_budget_99(self) -> None:
        assert LOOP_SUCCESS_SLO.error_budget == pytest.approx(0.01)

    def test_all_slos_count(self) -> None:
        assert len(ALL_SLOS) == 3


class TestBurnRateCalculator:
    def test_zero_error_rate(self) -> None:
        calc = BurnRateCalculator(LOOP_SUCCESS_SLO)
        assert calc.calculate(0.0) == 0.0

    def test_full_error_rate(self) -> None:
        calc = BurnRateCalculator(LOOP_SUCCESS_SLO)
        # 100% error / 1% budget = 100x burn rate
        assert calc.calculate(1.0) == pytest.approx(100.0)

    def test_at_budget_rate(self) -> None:
        calc = BurnRateCalculator(LOOP_SUCCESS_SLO)
        # error rate == error budget → burn rate = 1.0
        assert calc.calculate(0.01) == pytest.approx(1.0)

    def test_check_windows_no_alert(self) -> None:
        calc = BurnRateCalculator(LOOP_SUCCESS_SLO)
        # burn rate 1x, far below both thresholds
        alerts = calc.check_windows(0.01, 0.01)
        assert alerts == []

    def test_check_windows_critical_alert(self) -> None:
        calc = BurnRateCalculator(LOOP_SUCCESS_SLO)
        # 15% error rate → burn rate 15x > 14.4x critical threshold
        # 也超过 6.0x warning threshold，所以两个窗口都触发
        alerts = calc.check_windows(0.15, 0.15)
        assert len(alerts) == 2
        levels = {a.level for a in alerts}
        assert SLOLevel.CRITICAL in levels
        assert all(a.slo_name == "loop_success_rate" for a in alerts)

    def test_check_windows_warning_only_alert(self) -> None:
        calc = BurnRateCalculator(LOOP_SUCCESS_SLO)
        # 7% error rate → burn rate 7x > 6.0x warning but < 14.4x critical
        alerts = calc.check_windows(0.07, 0.07)
        assert len(alerts) == 1
        assert alerts[0].level == SLOLevel.WARNING

    def test_check_windows_both_alerts(self) -> None:
        calc = BurnRateCalculator(LOOP_SUCCESS_SLO)
        # 20% error rate → burn rate 20x > both thresholds
        alerts = calc.check_windows(0.20, 0.20)
        assert len(alerts) == 2
        levels = {a.level for a in alerts}
        assert levels == {SLOLevel.CRITICAL, SLOLevel.WARNING}

    def test_check_windows_long_short_must_both_exceed(self) -> None:
        """只有长窗口超阈值但短窗口不超 → 不告警。"""
        calc = BurnRateCalculator(LOOP_SUCCESS_SLO)
        # long=20% (200x), short=0% (0x)
        alerts = calc.check_windows(0.20, 0.0)
        assert alerts == []

    def test_alert_message_contains_slo_name(self) -> None:
        calc = BurnRateCalculator(LOOP_SUCCESS_SLO)
        alerts = calc.check_windows(0.15, 0.15)
        assert "loop_success_rate" in alerts[0].message

    def test_alert_has_window_durations(self) -> None:
        calc = BurnRateCalculator(LOOP_SUCCESS_SLO)
        alerts = calc.check_windows(0.15, 0.15)
        critical = next(a for a in alerts if a.level == SLOLevel.CRITICAL)
        assert critical.long_window_hours == 1.0
        assert critical.short_window_minutes == 5.0


class TestSLOAlert:
    def test_frozen_dataclass(self) -> None:
        alert = SLOAlert(
            slo_name="test",
            level=SLOLevel.CRITICAL,
            burn_rate=15.0,
            long_window_hours=1.0,
            short_window_minutes=5.0,
        )
        with pytest.raises(AttributeError):
            alert.slo_name = "other"  # type: ignore[misc]

    def test_default_attribution_fields(self) -> None:
        alert = SLOAlert(
            slo_name="test",
            level=SLOLevel.WARNING,
            burn_rate=7.0,
            long_window_hours=6.0,
            short_window_minutes=30.0,
        )
        assert alert.project == ""
        assert alert.model == ""
        assert alert.assertion == ""
