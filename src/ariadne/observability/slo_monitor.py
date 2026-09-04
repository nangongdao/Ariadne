"""周期 SLO 计算器 —— 把 slo.py 的纯计算接上真实数据源。

slo.py 只有计算逻辑（burn rate / 窗口判断），alerts.py 只有告警对象，
两者在生产里从未被调用 —— sli_query 从未发给 Prometheus，AlertManager
从未被实例化。结果是：M6 §9 验收项 #7"错误预算告警带归因"只存在于
代码里，生产环境永远不会告警。

本模块补上缺失的接线：
- 周期拉 Prometheus HTTP API（/api/v1/query），执行 SLOSpec.error_query，
  {{window}} 替换为窗口时长（小时）
- 用查询结果喂 BurnRateCalculator.check_windows（长短窗口都超阈值才告警）
- 触发时 AlertManager.create_alert + dispatch；AlertManager 自带去重

优雅降级：Prometheus 不可达 / 查询失败 / 返回多序列时跳过本轮 ——
SLO 监控自身不应成为可用性短板，宁可不告警也不误告警。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from urllib.parse import urlencode

import httpx

from ariadne.observability.alerts import Alert, AlertAttribution, AlertManager
from ariadne.observability.slo import (
    ALL_SLOS,
    BurnRateCalculator,
    SLOAlert,
    SLOSpec,
)
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)


class PrometheusClient:
    """Prometheus HTTP API 客户端。拉取瞬时查询结果。"""

    def __init__(self, base_url: str, timeout_seconds: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    async def query(self, promql: str) -> float | None:
        """执行 Instant Query，返回标量或单序列向量的值。

        空结果 / 多序列（查询没聚合维度）/ 错误返回 None。
        多序列直接跳过：每个序列的错误率不同，取第一个会漏掉其他维度
        的告警，静默评估单序列比不评估更危险。
        """
        url = self._base_url + "/api/v1/query?" + urlencode({"query": promql})
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        if data.get("status") != "success":
            return None
        payload = data.get("data", {})
        result_type = payload.get("resultType")
        result = payload.get("result")

        if result_type == "scalar":
            # 标量: result 是 [timestamp, "value"]
            return float(result[1])
        if result_type == "vector":
            if not result:
                return None
            if len(result) > 1:
                return None
            return float(result[0].get("value", [0, "0"])[1])
        return None

    @staticmethod
    def render_query(spec: SLOSpec, hours: float) -> str:
        """把 {{window}} 占位符替换为窗口时长（小时）。

        SLOSpec 的 PromQL 里占位符已经在方括号内（...rate(...[{{window}}]))），
        替换成 "1h" 即得 [1h]；短窗口 5m 折算 0.083h 同样合法。
        """
        return spec.error_query.replace("{{window}}", f"{hours}h")


class SLOMonitor:
    """周期计算多窗口燃烧率并派发告警。"""

    def __init__(
        self,
        *,
        prom: PrometheusClient,
        specs: tuple[SLOSpec, ...] = ALL_SLOS,
        interval_seconds: float = 60.0,
        manager: AlertManager | None = None,
    ) -> None:
        self._prom = prom
        self._specs = specs
        self._interval = interval_seconds
        self._manager = manager or AlertManager()
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while self._running:
            try:
                await self._check_once()
            except Exception as exc:
                logger.warning("slo check failed", extra={"error": str(exc)})
            await asyncio.sleep(self._interval)

    async def _check_once(self) -> None:
        """评估所有 SLO 的所有窗口；任一查询没结果就跳过该窗口。"""
        for spec in self._specs:
            for window in spec.windows:
                long_error = await self._prom.query(
                    PrometheusClient.render_query(spec, window.long_window_hours)
                )
                short_error = await self._prom.query(
                    PrometheusClient.render_query(
                        spec, window.short_window_minutes / 60.0
                    )
                )
                if long_error is None or short_error is None:
                    continue
                calculator = BurnRateCalculator(spec)
                for alert in calculator.check_windows(long_error, short_error):
                    self._dispatch(alert)

    def _dispatch(self, slo_alert: SLOAlert) -> None:
        """构建带归因的完整告警并派发（AlertManager 去重）。"""
        attribution = AlertAttribution(
            project_id=None,
            model=slo_alert.model or "",
            assertion=slo_alert.assertion or "",
        )
        self._manager.dispatch(self._manager.create_alert(slo_alert, attribution))

    @property
    def active(self) -> list[Alert]:
        return self._manager.active_alerts


__all__ = ["PrometheusClient", "SLOMonitor"]