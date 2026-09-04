"""FastAPI 应用装配。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from ariadne import __version__
from ariadne.api.errors import ApiError, api_error_handler, validation_error_handler
from ariadne.api.middleware.metrics import MetricsMiddleware
from ariadne.api.routers import (
    approvals,
    audit,
    costs,
    datasets,
    experiments,
    graphs,
    ingest,
    keys,
    loops,
    models,
    playground,
    retention,
    rules,
    specs,
    traces,
)
from ariadne.api.sse import router as sse_router
from ariadne.api.static import mount_static
from ariadne.auth.rls import verify_rls
from ariadne.config import Settings, get_settings, validate_production_secrets
from ariadne.storage.clickhouse import ClickHouseStore
from ariadne.storage.postgres.engine import PostgresStore
from ariadne.storage.queue import SpanQueue
from ariadne.utils.logging import configure_logging, get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    store = ClickHouseStore(settings.clickhouse)
    queue = SpanQueue(settings.redis)
    pg = PostgresStore(settings.postgres)
    app.state.store = store
    app.state.queue = queue
    app.state.pg = pg

    # 幂等创建消费者组：API 先起也不会因为组不存在而丢消息
    await queue.ensure_group()
    # RLS 失效是静默的（owner 回落 / 迁移未跑完都不报错），启动时主动问一次
    await verify_rls(pg)

    # GraphWorker 初始化（MVP：后台任务池）
    from ariadne.worker.graph_worker_singleton import (
        init_graph_worker,
        shutdown_graph_worker,
    )

    init_graph_worker(pg, settings)

    # GraphQueue 初始化（阶段 2：Redis 队列）
    from ariadne.worker.graph_queue_singleton import (
        close_graph_queue,
        init_graph_queue,
    )

    init_graph_queue(settings)

    # SLO 监控：Prometheus URL 配置时才启动（本地开发可能无 Prometheus）
    slo_monitor = None
    if settings.observability.prometheus_url:
        from ariadne.observability.slo_monitor import PrometheusClient, SLOMonitor

        prom = PrometheusClient(settings.observability.prometheus_url)
        slo_monitor = SLOMonitor(
            prom=prom, interval_seconds=settings.observability.slo_interval_seconds
        )
        await slo_monitor.start()
        logger.info("slo monitor started")

    logger.info(
        "api started",
        extra={"version": __version__, "env": settings.env,
               "project_id": str(settings.api.default_project_id)},
    )
    try:
        yield
    finally:
        if slo_monitor is not None:
            await slo_monitor.stop()
        await shutdown_graph_worker()
        await close_graph_queue()
        await queue.close()
        await pg.close()
        store.close()
        logger.info("api stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    configure_logging(resolved.log_level)
    # 生产环境带着出厂密钥启动等于没有认证，且日志里看不出异常。放在建 app
    # 之前：绑端口之后再拒绝，编排器会当成健康检查失败而反复重启。
    validate_production_secrets(resolved)

    app = FastAPI(
        title="Ariadne API",
        version=__version__,
        description="AI 工作流质量保障与可观测性平台",
        lifespan=lifespan,
    )
    app.state.settings = resolved

    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.api.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(MetricsMiddleware)

    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)

    app.include_router(ingest.router, prefix="/v1")
    app.include_router(traces.router, prefix="/v1")
    app.include_router(costs.router, prefix="/v1")
    app.include_router(datasets.router, prefix="/v1")
    app.include_router(experiments.router, prefix="/v1")
    app.include_router(loops.router, prefix="/v1")
    app.include_router(rules.router, prefix="/v1")
    app.include_router(specs.router, prefix="/v1")
    app.include_router(approvals.router, prefix="/v1")
    app.include_router(graphs.router, prefix="/v1")
    app.include_router(keys.router, prefix="/v1")
    app.include_router(audit.router, prefix="/v1")
    app.include_router(models.router, prefix="/v1")
    app.include_router(playground.router, prefix="/v1")
    app.include_router(retention.router, prefix="/v1")
    app.include_router(sse_router, prefix="/v1")

    # P2-11：可选的元端点认证
    # require_meta_auth=False（默认）：公开，便于 Prometheus/K8s probe
    # require_meta_auth=True：要求 API Key，避免暴露依赖状态/队列深度
    from fastapi import Depends

    from ariadne.api import deps as api_deps

    meta_deps = (
        [Depends(api_deps.require_project)]
        if settings and settings.api.require_meta_auth
        else []
    )

    @app.get("/health", tags=["meta"], summary="健康检查", dependencies=meta_deps)
    async def health() -> dict[str, Any]:
        """依赖不可用时返回 degraded 而非 500：便于编排系统区分
        "进程活着但依赖挂了"和"进程死了"。

        认证：由 ARIADNE_API_REQUIRE_META_AUTH 控制（默认 False）。
        """
        store: ClickHouseStore = app.state.store
        queue: SpanQueue = app.state.queue
        pg: PostgresStore = app.state.pg
        ch_ok = store.ping()
        redis_ok = await queue.ping()
        pg_ok = await pg.ping()
        return {
            "status": "ok" if (ch_ok and redis_ok and pg_ok) else "degraded",
            "version": __version__,
            "clickhouse": ch_ok,
            "postgres": pg_ok,
            "redis": redis_ok,
        }

    @app.get("/v1/stats", tags=["meta"], summary="采集管道状态", dependencies=meta_deps)
    async def stats() -> dict[str, Any]:
        """队列深度统计。

        认证：由 ARIADNE_API_REQUIRE_META_AUTH 控制（默认 False）。
        """
        queue: SpanQueue = app.state.queue
        return {
            "queue_length": await queue.stream_length(),
            "pending": await queue.pending_count(),
        }

    @app.get("/metrics", tags=["meta"], summary="Prometheus 指标", dependencies=meta_deps)
    async def metrics() -> Response:
        """Prometheus 文本格式指标暴露端点。

        Prometheus 抓取此端点采集 ariadne_* 指标族。
        M6 §9 验收项 #7：指标驱动 SLO 告警。

        认证：由 ARIADNE_API_REQUIRE_META_AUTH 控制（默认 False）。
        """
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )

    # 必须最后挂载：SPA 回落路由是 catch-all，会吞掉之后注册的任何路径
    mount_static(app)

    return app


app = create_app()
