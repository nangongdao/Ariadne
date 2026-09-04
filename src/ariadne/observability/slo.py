"""多窗口燃烧率 SLO 引擎。

M6 §9 验收项 #7：错误预算告警带归因。

多窗口燃烧率（Multi-window burn rate）是 Google SRE 推荐的告警策略：
同时在长短两个窗口检测错误率是否超过预算消耗速率，减少误报的同时保持灵敏度。

常见双窗口组合：
  - 1h + 5m（快速检测，允许一定误报）
  - 6h + 30m（慢速检测，误报率低）

告警条件：长窗口和短窗口的错误率都超过目标燃烧速率（通常为预算的 14.4 倍）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SLOLevel(StrEnum):
    """SLO 严重级别。"""

    CRITICAL = "critical"  # 2% 预算消耗（1h 窗口）
    WARNING = "warning"    # 10% 预算消耗（6h 窗口）


@dataclass(frozen=True)
class SLOWindow:
    """SLO 检测窗口定义。"""

    long_window_hours: float
    short_window_minutes: float
    # 燃烧率阈值 = 1 / error_budget_fraction
    # 例如 99.9% SLO 的 2% 预算在 1h 窗口 = 14.4 倍燃烧率
    burn_rate_threshold: float
    level: SLOLevel


# 标准多窗口配置（99.9% SLO）
DEFAULT_WINDOWS: tuple[SLOWindow, ...] = (
    SLOWindow(
        long_window_hours=1.0,
        short_window_minutes=5.0,
        burn_rate_threshold=14.4,  # 2% 预算在 1h 内消耗
        level=SLOLevel.CRITICAL,
    ),
    SLOWindow(
        long_window_hours=6.0,
        short_window_minutes=30.0,
        burn_rate_threshold=6.0,   # 10% 预算在 6h 内消耗
        level=SLOLevel.WARNING,
    ),
)


@dataclass(frozen=True)
class SLOSpec:
    """SLO 规格定义。

    target: 目标成功率（如 0.999 表示 99.9%）
    """

    name: str
    target: float
    # SLI 查询表达式（Prometheus PromQL），{{window}} 占位符替换为窗口时长
    sli_query: str
    # 错误率查询（1 - 成功率）
    error_query: str
    windows: tuple[SLOWindow, ...] = DEFAULT_WINDOWS

    @property
    def error_budget(self) -> float:
        """错误预算 = 1 - target。"""
        return 1.0 - self.target


@dataclass(frozen=True)
class SLOAlert:
    """SLO 告警。"""

    slo_name: str
    level: SLOLevel
    burn_rate: float
    long_window_hours: float
    short_window_minutes: float
    # 归因维度（M6 §9 验收项 #7）
    project: str = ""
    model: str = ""
    assertion: str = ""
    message: str = ""


@dataclass
class BurnRateCalculator:
    """燃烧率计算器。

    burn_rate = observed_error_rate / expected_error_rate
    其中 expected_error_rate = error_budget = 1 - target
    """

    slo: SLOSpec

    def calculate(self, observed_error_rate: float) -> float:
        """计算当前观测错误率的燃烧率。

        burn_rate = observed_error_rate / error_budget
        """
        if self.slo.error_budget <= 0:
            return 0.0
        return observed_error_rate / self.slo.error_budget

    def check_windows(
        self,
        long_window_error_rate: float,
        short_window_error_rate: float,
    ) -> list[SLOAlert]:
        """检查所有窗口是否触发告警。

        返回触发的告警列表（可能为空）。
        """
        alerts: list[SLOAlert] = []
        long_burn = self.calculate(long_window_error_rate)
        short_burn = self.calculate(short_window_error_rate)

        for window in self.slo.windows:
            if long_burn >= window.burn_rate_threshold and short_burn >= window.burn_rate_threshold:
                alerts.append(
                    SLOAlert(
                        slo_name=self.slo.name,
                        level=window.level,
                        burn_rate=max(long_burn, short_burn),
                        long_window_hours=window.long_window_hours,
                        short_window_minutes=window.short_window_minutes,
                        message=(
                            f"SLO {self.slo.name} burn rate "
                            f"{max(long_burn, short_burn):.1f}x "
                            f"exceeds threshold "
                            f"{window.burn_rate_threshold}x "
                            f"({window.long_window_hours}h + "
                            f"{window.short_window_minutes}m windows)"
                        ),
                    )
                )
        return alerts


# ---- 预定义 SLO 规格 ----

# Loop 闭环达标率 SLO（99% Loop 达标或合理终止）
LOOP_SUCCESS_SLO = SLOSpec(
    name="loop_success_rate",
    target=0.99,
    sli_query=(
        'sum(rate(ariadne_loop_terminal_total'
        '{final_state=~"converged|stopped|budget_exceeded"}'
        '[{{window}}])) '
        '/ sum(rate(ariadne_loop_terminal_total[{{window}}]))'
    ),
    error_query=(
        '1 - (sum(rate(ariadne_loop_terminal_total'
        '{final_state=~"converged|stopped|budget_exceeded"}'
        '[{{window}}])) '
        '/ sum(rate(ariadne_loop_terminal_total[{{window}}])))'
    ),
)

# 采集管道可用性 SLO（99.9% span 成功写入）
INGEST_AVAILABILITY_SLO = SLOSpec(
    name="ingest_availability",
    target=0.999,
    sli_query=(
        'sum(rate(ariadne_collector_written_total[{{window}}])) '
        '/ sum(rate(ariadne_collector_consumed_total[{{window}}]))'
    ),
    error_query=(
        '1 - (sum(rate(ariadne_collector_written_total[{{window}}])) '
        '/ sum(rate(ariadne_collector_consumed_total[{{window}}]))'
    ),
)

# API 延迟 SLO（99% 请求 < 200ms）
API_LATENCY_SLO = SLOSpec(
    name="api_latency_p99",
    target=0.99,
    sli_query=(
        'histogram_quantile(0.99, '
        'sum(rate(ariadne_api_request_duration_seconds_bucket'
        '[{{window}}])) by (le)) < 0.2'
    ),
    error_query=(
        '1 - (histogram_quantile(0.99, '
        'sum(rate(ariadne_api_request_duration_seconds_bucket'
        '[{{window}}])) by (le)) < 0.2)'
    ),
)


ALL_SLOS: tuple[SLOSpec, ...] = (
    LOOP_SUCCESS_SLO,
    INGEST_AVAILABILITY_SLO,
    API_LATENCY_SLO,
)


__all__ = [
    "ALL_SLOS",
    "API_LATENCY_SLO",
    "DEFAULT_WINDOWS",
    "INGEST_AVAILABILITY_SLO",
    "LOOP_SUCCESS_SLO",
    "BurnRateCalculator",
    "SLOAlert",
    "SLOLevel",
    "SLOSpec",
    "SLOWindow",
]
