"""托管前端构建产物。

由 API 直接托管而非单起 nginx：M1 的目标是"一键起"，少一个容器少一个
端口。控制台是低流量内部页面，静态文件服务的性能开销可忽略。
生产规模上量后可在前面挂 CDN 或反代，不影响这里的实现。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

# 容器内为 /app/web/dist；本地开发为仓库根的 web/dist
_CANDIDATES = (
    Path("/app/web/dist"),
    Path(__file__).resolve().parents[3] / "web" / "dist",
)


def find_static_dir() -> Path | None:
    for candidate in _CANDIDATES:
        if (candidate / "index.html").is_file():
            return candidate
    return None


def mount_static(app: FastAPI) -> None:
    """挂载前端。构建产物不存在时静默跳过（纯 API 部署是合法用法）。"""
    static_dir = find_static_dir()
    if static_dir is None:
        logger.info("frontend build not found, serving API only")
        return

    # 带哈希文件名的资源可长期缓存
    app.mount(
        "/assets",
        StaticFiles(directory=static_dir / "assets"),
        name="assets",
    )

    index_file = static_dir / "index.html"

    @app.get("/", include_in_schema=False)
    async def serve_index() -> FileResponse:
        return FileResponse(index_file)

    # SPA 前端路由（/traces/xxx）刷新时会打到后端，需回落到 index.html
    # 由前端路由接管。API 路径已在此之前注册，不会被这条捕获。
    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback() -> FileResponse:
        return FileResponse(index_file)

    logger.info("frontend mounted", extra={"dir": str(static_dir)})
