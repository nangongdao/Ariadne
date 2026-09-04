"""告警管理器测试：去重、归因、序列化。

M6 §9 验收项 #7：错误预算告警带归因。
"""

from __future__ import annotations

import json
from uuid import UUID

import pytest

from ariadne.observability.alerts import (
    Alert,
    AlertAttribution,
    AlertManager,
    build_attribution,
)
from ariadne.observability.slo import SLOAlert, SLOLevel


class TestAlertAttribution:
    def test_empty_attribution(self) -> None:
        attr = AlertAttribution()
        assert attr.project_id is None
        assert attr.model == ""
        assert attr.assertion == ""

    def test_build_attribution_with_kwargs(self) -> None:
        pid = UUID("12345678-1234-5678-1234-567812345678")
        attr = build_attribution(
            project_id=pid,
            model="claude-sonnet",
            assertion="quality_score",
            provider="anthropic",
            rule_id="R001",
        )
        assert attr.project_id == pid
        assert attr.model == "claude-sonnet"
        assert attr.assertion == "quality_score"
        assert attr.provider == "anthropic"
        assert attr.extra == {"rule_id": "R001"}


class TestAlert:
    def test_frozen(self) -> None:
        alert = Alert(
            slo_name="test",
            level=SLOLevel.CRITICAL,
            burn_rate=15.0,
            message="test",
            attribution=AlertAttribution(),
        )
        with pytest.raises(AttributeError):
            alert.slo_name = "other"  # type: ignore[misc]

    def test_to_json_minimal(self) -> None:
        alert = Alert(
            slo_name="loop_success_rate",
            level=SLOLevel.WARNING,
            burn_rate=7.0,
            message="burn rate high",
            attribution=AlertAttribution(),
        )
        d = json.loads(alert.to_json())
        assert d["slo_name"] == "loop_success_rate"
        assert d["level"] == "warning"
        assert d["burn_rate"] == 7.0
        assert "project_id" not in d

    def test_to_json_with_attribution(self) -> None:
        pid = UUID("12345678-1234-5678-1234-567812345678")
        alert = Alert(
            slo_name="loop_success_rate",
            level=SLOLevel.CRITICAL,
            burn_rate=15.0,
            message="burn rate critical",
            attribution=AlertAttribution(
                project_id=pid,
                model="claude-sonnet",
                assertion="quality_score",
                provider="anthropic",
            ),
        )
        d = json.loads(alert.to_json())
        assert d["project_id"] == str(pid)
        assert d["model"] == "claude-sonnet"
        assert d["assertion"] == "quality_score"
        assert d["provider"] == "anthropic"

    def test_fingerprint_dedup(self) -> None:
        attr = AlertAttribution(model="claude-sonnet", assertion="quality")
        a1 = Alert(
            slo_name="test",
            level=SLOLevel.CRITICAL,
            burn_rate=15.0,
            message="msg",
            attribution=attr,
        )
        a2 = Alert(
            slo_name="test",
            level=SLOLevel.CRITICAL,
            burn_rate=20.0,
            message="other msg",
            attribution=attr,
        )
        assert a1.fingerprint == a2.fingerprint

    def test_fingerprint_differs_by_attribution(self) -> None:
        a1 = Alert(
            slo_name="test",
            level=SLOLevel.CRITICAL,
            burn_rate=15.0,
            message="msg",
            attribution=AlertAttribution(model="model_a", assertion="x"),
        )
        a2 = Alert(
            slo_name="test",
            level=SLOLevel.CRITICAL,
            burn_rate=15.0,
            message="msg",
            attribution=AlertAttribution(model="model_b", assertion="x"),
        )
        assert a1.fingerprint != a2.fingerprint


class TestAlertManager:
    def _make_slo_alert(self, level: SLOLevel = SLOLevel.CRITICAL) -> SLOAlert:
        return SLOAlert(
            slo_name="loop_success_rate",
            level=level,
            burn_rate=15.0,
            long_window_hours=1.0,
            short_window_minutes=5.0,
            message="test alert",
        )

    def test_dispatch_new_alert(self) -> None:
        mgr = AlertManager()
        alert = mgr.create_alert(self._make_slo_alert())
        assert mgr.dispatch(alert) is True
        assert len(mgr.active_alerts) == 1

    def test_dispatch_duplicate_skipped(self) -> None:
        mgr = AlertManager()
        alert = mgr.create_alert(self._make_slo_alert())
        assert mgr.dispatch(alert) is True
        assert mgr.dispatch(alert) is False
        assert len(mgr.active_alerts) == 1

    def test_resolve_alert(self) -> None:
        mgr = AlertManager()
        alert = mgr.create_alert(self._make_slo_alert())
        mgr.dispatch(alert)
        assert mgr.resolve(alert.fingerprint) is True
        assert len(mgr.active_alerts) == 0

    def test_resolve_unknown(self) -> None:
        mgr = AlertManager()
        assert mgr.resolve("nonexistent") is False

    def test_create_alert_with_attribution(self) -> None:
        mgr = AlertManager()
        attr = AlertAttribution(
            project_id=UUID("12345678-1234-5678-1234-567812345678"),
            model="claude-sonnet",
        )
        alert = mgr.create_alert(self._make_slo_alert(), attribution=attr)
        assert alert.attribution.model == "claude-sonnet"
        assert alert.attribution.project_id is not None

    def test_multiple_different_alerts(self) -> None:
        mgr = AlertManager()
        a1 = mgr.create_alert(
            self._make_slo_alert(SLOLevel.CRITICAL),
            AlertAttribution(model="model_a"),
        )
        a2 = mgr.create_alert(
            self._make_slo_alert(SLOLevel.WARNING),
            AlertAttribution(model="model_b"),
        )
        assert mgr.dispatch(a1) is True
        assert mgr.dispatch(a2) is True
        assert len(mgr.active_alerts) == 2
