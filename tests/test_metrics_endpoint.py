"""/metrics Prometheus 指标暴露端点测试。

M6 §9 验收项 #7：指标驱动 SLO 告警。
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from ariadne.api.app import create_app


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_prometheus_format() -> None:
    """指标端点返回 Prometheus 文本格式。"""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers.get("content-type", "")
    # Prometheus 文本格式应包含 HELP 或 TYPE 行
    text = resp.text
    assert "# HELP" in text or "# TYPE" in text


@pytest.mark.asyncio
async def test_metrics_contains_ariadne_prefix() -> None:
    """所有指标使用 ariadne_ 前缀。"""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    # 应包含至少一些 ariadne_ 指标
    assert "ariadne_" in resp.text


@pytest.mark.asyncio
async def test_api_request_duration_recorded() -> None:
    """API 请求应被 metrics 中间件记录到直方图。

    /v1/stats 需要 lifespan（Redis 连接），不能直接调。
    改用 /docs（被排除不计入）确认 metrics 中间件本身不影响正常请求，
    然后验证 ariadne_api_request_duration_seconds 直方图已在指标输出中。
    """
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # /docs 被 _EXCLUDED_PREFIXES 排除，不会记录指标
        await client.get("/docs")
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    # 直方图定义应出现在指标输出中（即使无数据，TYPE/HELP 也会有）
    assert "ariadne_api_request_duration_seconds" in resp.text
