"""Prometheus 指标采集中间件。

记录每个 API 请求的耗时到 ariadne_api_request_duration_seconds 直方图。
这是唯一一处自动采集的指标 —— 业务指标（Loop/Harness/Eval/Collector）
在各自代码路径里显式 .inc()/.observe()。

用 Starlette middleware 而非 FastAPI dependency：dependency 只对路由生效，
middleware 覆盖所有请求（含 404、静态资源）。但静态资源请求不计入 API 指标
（path 已过滤）。
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from ariadne.observability.metrics import api_request_duration_seconds

# 不计入 API 指标的路径前缀（静态资源、健康检查、指标端点自身）
_EXCLUDED_PREFIXES: frozenset[str] = frozenset(
    {"/assets/", "/health", "/metrics", "/openapi", "/docs", "/redoc"}
)


class MetricsMiddleware(BaseHTTPMiddleware):
    """记录 API 请求耗时到 Prometheus 直方图。"""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # 静态资源和 meta 端点不计入 API 指标
        path = request.url.path
        if any(path.startswith(prefix) for prefix in _EXCLUDED_PREFIXES):
            return await call_next(request)

        method = request.method
        start = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            # 异常也记录（status=500），然后重新抛出让全局异常处理器处理
            elapsed = time.monotonic() - start
            api_request_duration_seconds.labels(
                method=method,
                path=path,
                status="500",
            ).observe(elapsed)
            raise

        elapsed = time.monotonic() - start
        api_request_duration_seconds.labels(
            method=method,
            path=path,
            status=str(response.status_code),
        ).observe(elapsed)
        return response
