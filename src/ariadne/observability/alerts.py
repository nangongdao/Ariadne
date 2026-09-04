"""告警生成 + 归因信息。

M6 §9 验收项 #7：错误预算告警带归因。
告警必须携带 project / model / assertion 维度信息，
让 oncall 能快速定位是哪个项目的哪个模型/断言出了问题。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import UUID

from ariadne.observability.slo import SLOAlert, SLOLevel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertAttribution:
    """告警归因维度。"""

    project_id: UUID | None = None
    model: str = ""
    assertion: str = ""
    provider: str = ""
    # 附加上下文（如 rule_id、error_code 等）
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Alert:
    """完整告警。SLO 告警 + 归因。"""

    slo_name: str
    level: SLOLevel
    burn_rate: float
    message: str
    attribution: AlertAttribution
    long_window_hours: float = 0.0
    short_window_minutes: float = 0.0

    def to_json(self) -> str:
        """序列化为 JSON（用于 Alertmanager webhook 或日志）。"""
        d: dict[str, Any] = {
            "slo_name": self.slo_name,
            "level": self.level.value,
            "burn_rate": self.burn_rate,
            "message": self.message,
            "long_window_hours": self.long_window_hours,
            "short_window_minutes": self.short_window_minutes,
        }
        if self.attribution.project_id:
            d["project_id"] = str(self.attribution.project_id)
        if self.attribution.model:
            d["model"] = self.attribution.model
        if self.attribution.assertion:
            d["assertion"] = self.attribution.assertion
        if self.attribution.provider:
            d["provider"] = self.attribution.provider
        if self.attribution.extra:
            d["extra"] = self.attribution.extra
        return json.dumps(d, ensure_ascii=False)

    @property
    def fingerprint(self) -> str:
        """告警指纹（用于去重）。"""
        return f"{self.slo_name}:{self.level}:{self.attribution.model}:{self.attribution.assertion}"


class AlertManager:
    """告警管理器。负责告警去重 + 归因注入 + 发送。

    实际发送通过 Alertmanager webhook（生产）或日志（开发）。
    """

    def __init__(self) -> None:
        # 已发告警指纹集合（去重）
        self._active: dict[str, Alert] = {}

    def create_alert(
        self,
        slo_alert: SLOAlert,
        attribution: AlertAttribution | None = None,
    ) -> Alert:
        """从 SLO 告警 + 归因创建完整告警。"""
        return Alert(
            slo_name=slo_alert.slo_name,
            level=slo_alert.level,
            burn_rate=slo_alert.burn_rate,
            message=slo_alert.message,
            attribution=attribution or AlertAttribution(),
            long_window_hours=slo_alert.long_window_hours,
            short_window_minutes=slo_alert.short_window_minutes,
        )

    def dispatch(self, alert: Alert) -> bool:
        """发送告警。

        返回 True 表示是新告警（首次触发），False 表示已激活（去重）。
        生产环境将通过 webhook 发送到 Alertmanager。
        """
        if alert.fingerprint in self._active:
            logger.debug(
                "alert already active, skipping",
                extra={"fingerprint": alert.fingerprint},
            )
            return False

        self._active[alert.fingerprint] = alert
        logger.warning(
            "SLO alert triggered",
            extra={
                "slo": alert.slo_name,
                "level": alert.level.value,
                "burn_rate": alert.burn_rate,
                "attribution": asdict(alert.attribution),
            },
        )
        return True

    def resolve(self, fingerprint: str) -> bool:
        """解决（清除）一个已激活告警。"""
        if fingerprint in self._active:
            alert = self._active.pop(fingerprint)
            logger.info(
                "SLO alert resolved",
                extra={
                    "slo": alert.slo_name,
                    "fingerprint": fingerprint,
                },
            )
            return True
        return False

    @property
    def active_alerts(self) -> list[Alert]:
        return list(self._active.values())


def build_attribution(
    project_id: UUID | None = None,
    model: str = "",
    assertion: str = "",
    provider: str = "",
    **extra: str,
) -> AlertAttribution:
    """构建告警归因。"""
    return AlertAttribution(
        project_id=project_id,
        model=model,
        assertion=assertion,
        provider=provider,
        extra=extra,
    )


__all__ = [
    "Alert",
    "AlertAttribution",
    "AlertManager",
    "build_attribution",
]
