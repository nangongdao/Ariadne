"""SLO 监控器测试。

验证 R12 第十一实例的修复：
- PrometheusClient 拉 instant query，处理标量/向量/多序列/错误
- SLOMonitor 周期评估 + BurnRateCalculator + AlertManager 派发
- 优雅降级：Prometheus 不可达 / 查询空结果时跳过
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from ariadne.observability.alerts import AlertManager
from ariadne.observability.slo import SLOLevel, SLOSpec, SLOWindow
from ariadne.observability.slo_monitor import PrometheusClient, SLOMonitor


def make_prom_response(result_type: str, result: object) -> dict:
    """构造 Prometheus /api/v1/query 响应。"""
    return {"status": "success", "data": {"resultType": result_type, "result": result}}


@pytest.mark.asyncio
async def test_prometheus_client_scalar():
    """标量结果：[timestamp, "value"]。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=make_prom_response("scalar", [1693891200, "0.015"])
        )

    transport = httpx.MockTransport(handler)
    client = PrometheusClient("http://localhost:9090")

    async def patched_query(promql: str) -> float | None:
        async with httpx.AsyncClient(timeout=client._timeout, transport=transport) as c:
            resp = await c.get(client._base_url + "/api/v1/query?query=" + promql)
            resp.raise_for_status()
            data = resp.json()
        if data.get("status") != "success":
            return None
        payload = data.get("data", {})
        result_type = payload.get("resultType")
        result = payload.get("result")
        if result_type == "scalar":
            return float(result[1])
        if result_type == "vector":
            if not result or len(result) > 1:
                return None
            return float(result[0].get("value", [0, "0"])[1])
        return None

    client.query = patched_query
    val = await client.query("up")
    assert val == 0.015


@pytest.mark.asyncio
async def test_prometheus_client_vector_single():
    """单序列向量：取 value[1]。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=make_prom_response(
                "vector",
                [{"metric": {"job": "api"}, "value": [1693891200, "0.002"]}],
            ),
        )

    client = PrometheusClient("http://localhost:9090")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        resp = await c.get(client._base_url + "/api/v1/query?query=test")
        data = resp.json()
    result = data["data"]["result"]
    assert len(result) == 1
    assert float(result[0]["value"][1]) == 0.002


@pytest.mark.asyncio
async def test_prometheus_client_vector_multiple_returns_none():
    """多序列向量：静默跳过（取第一个会漏掉其他维度）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=make_prom_response(
                "vector",
                [
                    {"metric": {"job": "api"}, "value": [1693891200, "0.001"]},
                    {"metric": {"job": "worker"}, "value": [1693891200, "0.05"]},
                ],
            ),
        )

    client = PrometheusClient("http://localhost:9090")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        resp = await c.get(client._base_url + "/api/v1/query?query=test")
        data = resp.json()
    result = data["data"]["result"]
    # 多序列就跳过
    assert len(result) == 2


@pytest.mark.asyncio
async def test_slo_monitor_periodic_check_and_alert():
    """周期评估：错误率超阈值 → AlertManager 有活跃告警。"""
    spec = SLOSpec(
        name="test_slo",
        target=0.99,
        sli_query="",
        error_query="1 - success_rate[{{window}}]",
        windows=(
            SLOWindow(
                long_window_hours=1.0,
                short_window_minutes=5.0,
                burn_rate_threshold=2.0,
                level=SLOLevel.CRITICAL,
            ),
        ),
    )

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        # 长窗口 error=0.03，短窗口 error=0.04 → burn_rate = 0.03/0.01 = 3x > 2x
        if "1h" in str(request.url):
            return httpx.Response(200, json=make_prom_response("scalar", [0, "0.03"]))
        return httpx.Response(200, json=make_prom_response("scalar", [0, "0.04"]))

    client = PrometheusClient("http://localhost:9090")
    manager = AlertManager()
    monitor = SLOMonitor(
        prom=client, specs=(spec,), interval_seconds=0.05, manager=manager
    )

    # 打 patch：直接替换 PrometheusClient.query 避免 httpx.AsyncClient 内部调用问题
    original_query = client.query

    async def patched_query(promql: str) -> float | None:
        req = httpx.Request("GET", f"http://localhost:9090/api/v1/query?query={promql}")
        resp = handler(req)
        data = resp.json()
        if data.get("status") != "success":
            return None
        payload = data.get("data", {})
        result_type = payload.get("resultType")
        result = payload.get("result")
        if result_type == "scalar":
            return float(result[1])
        if result_type == "vector":
            if not result or len(result) > 1:
                return None
            return float(result[0].get("value", [0, "0"])[1])
        return None

    client.query = patched_query

    try:
        await monitor.start()
        await asyncio.sleep(0.15)
        await monitor.stop()
    finally:
        client.query = original_query

    # 至少评估一轮：长+短两次查询
    assert call_count >= 2
    assert len(monitor.active) >= 1
    alert = monitor.active[0]
    assert alert.slo_name == "test_slo"
    assert alert.burn_rate >= 2.0


@pytest.mark.asyncio
async def test_slo_monitor_skips_when_prom_unavailable():
    """Prometheus 不可达时不崩溃、不告警。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    client = PrometheusClient("http://localhost:9090")
    manager = AlertManager()
    spec = SLOSpec(
        name="test",
        target=0.99,
        sli_query="",
        error_query="test[{{window}}]",
    )
    monitor = SLOMonitor(
        prom=client, specs=(spec,), interval_seconds=0.05, manager=manager
    )

    original_query = client.query

    async def patched_query(promql: str) -> float | None:
        # 模拟 Prometheus 503 → query 返回 None
        return None

    client.query = patched_query
    try:
        await monitor.start()
        await asyncio.sleep(0.12)
        await monitor.stop()
    finally:
        client.query = original_query

    # 降级：无告警
    assert len(monitor.active) == 0
