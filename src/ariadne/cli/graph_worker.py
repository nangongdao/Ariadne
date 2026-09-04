#!/usr/bin/env python3
"""Graph Worker 进程入口（阶段 2）。

独立 Worker 进程，从 Redis Streams 消费 Graph 执行任务。

使用方式：
    python -m ariadne.cli.graph_worker

或通过 Docker Compose / Kubernetes 部署多副本。
"""

import asyncio
import logging
import signal
import sys
from typing import Any

from ariadne.config import Settings
from ariadne.storage.postgres.engine import PostgresStore
from ariadne.worker.graph_queue import GraphQueue
from ariadne.worker.graph_worker_v2 import GraphWorkerV2

logger = logging.getLogger(__name__)


async def main() -> None:
    """Graph Worker 主入口。"""
    settings = Settings()

    # 初始化存储
    pg = PostgresStore(settings.postgres)

    # 初始化队列
    queue = GraphQueue(settings.redis)
    await queue.ensure_group()

    # 创建 Worker
    worker = GraphWorkerV2(
        pg_factory=pg,
        settings=settings,
        queue=queue,
    )

    # 信号处理：优雅关闭
    shutdown_event = asyncio.Event()

    def handle_signal(sig: int) -> None:
        logger.info(f"收到信号 {sig}，准备关闭...")
        shutdown_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))  # type: ignore[misc]

    logger.info("Graph Worker 启动")

    try:
        # 启动 Worker（阻塞直到 shutdown_event 触发）
        worker_task = asyncio.create_task(worker.run())

        # 等待关闭信号
        await shutdown_event.wait()

        # 优雅关闭
        await worker.shutdown()
        await worker_task

    except KeyboardInterrupt:
        logger.info("收到 Ctrl+C，关闭...")
    finally:
        await queue.close()
        await pg.close()
        logger.info("Graph Worker 已关闭")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
