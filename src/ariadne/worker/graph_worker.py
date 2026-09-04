"""GraphWorker：异步 Graph 执行器（MVP 简化版）。

MVP 简化：
- 后台任务池（asyncio.create_task），不走 Redis 队列
- 无租约机制，不支持多 Worker 并发
- 无检查点恢复

后续阶段 2 升级为 Worker 池 + Redis 队列 + 租约机制。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from ariadne.graph_module.executor import GraphExecutor
from ariadne.graph_module.runtime import build_node_executors_from_settings
from ariadne.graph_module.serialize import spec_to_graph
from ariadne.runtime_module.llm.resolver import resolve_project_llm
from ariadne.storage.postgres.repositories.graph_runs import (
    GraphRunNotFoundError,
    GraphRunRepository,
)
from ariadne.storage.postgres.repositories.graphs import GraphRepository

if TYPE_CHECKING:
    from ariadne.config import Settings
    from ariadne.storage.postgres.engine import PostgresStore

logger = logging.getLogger(__name__)


class GraphWorker:
    """Graph 异步执行器（MVP：后台任务池）。"""

    def __init__(
        self,
        pg_factory: PostgresStore,
        settings: Settings,
    ) -> None:
        self._pg_factory = pg_factory
        self._settings = settings
        self._tasks: set[asyncio.Task[None]] = set()
        self._max_concurrent = 10
        self._semaphore = asyncio.Semaphore(self._max_concurrent)

    async def submit(
        self,
        graph_run_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> None:
        """提交后台任务执行图。"""
        task = asyncio.create_task(
            self._execute_with_semaphore(graph_run_id, project_id)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _execute_with_semaphore(
        self,
        graph_run_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> None:
        """带并发控制的执行入口。"""
        async with self._semaphore:
            await self._execute(graph_run_id, project_id)

    async def _execute(
        self,
        graph_run_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> None:
        """执行单个 graph run（后台任务）。"""
        async with self._pg_factory.session() as session:
            graph_runs_repo = GraphRunRepository(session)
            graphs_repo = GraphRepository(session)

            try:
                # 1. 读取 graph_run + graph 定义
                run = await graph_runs_repo.by_id(graph_run_id)
                graph_row = await graphs_repo.get(
                    project_id=project_id, graph_id=run.graph_id
                )

                # 2. transition(RUNNING)
                await graph_runs_repo.transition(graph_run_id, "RUNNING")
                await session.commit()

                # 3. 装配 node_executors
                graph = spec_to_graph(graph_row.graph)

                # 只在图中有 LLM/Loop 节点时才 resolve 项目模型
                from ariadne.graph_module.models import NodeKind
                needs_llm = any(
                    node.kind in (NodeKind.LLM, NodeKind.LOOP)
                    for node in graph.nodes
                )

                if needs_llm:
                    resolved_llm, resolved_model = await resolve_project_llm(
                        self._pg_factory, project_id, env_settings=self._settings.llm
                    )
                    node_executors = build_node_executors_from_settings(
                        self._settings, llm=resolved_llm, default_model=resolved_model
                    )
                else:
                    resolved_llm = None
                    resolved_model = None
                    node_executors = build_node_executors_from_settings(self._settings)

                # 4. 执行 GraphExecutor.run()
                try:
                    exec_result = await GraphExecutor().run(
                        graph, inputs=run.inputs, node_executors=node_executors
                    )

                    # 5. finish(COMPLETED/FAILED)
                    final_state = "FAILED" if exec_result.errors else "COMPLETED"
                    await graph_runs_repo.finish(
                        graph_run_id=graph_run_id,
                        final_state=final_state,
                        outputs=exec_result.outputs,
                        node_states={
                            k: v.value for k, v in exec_result.node_states.items()
                        },
                        errors=exec_result.errors,
                    )
                    await session.commit()

                    logger.info(
                        f"Graph run {graph_run_id} 完成: {final_state}, "
                        f"{len(exec_result.outputs)} outputs, "
                        f"{len(exec_result.errors)} errors"
                    )

                finally:
                    # 清理 LLM client（仅在 resolved_llm 不为 None 时）
                    if resolved_llm is not None:
                        close = getattr(resolved_llm, "aclose", None)
                        if close is not None:
                            await close()

            except TimeoutError:
                # 超时处理（当前 GraphExecutor.run() 无超时机制，预留）
                await graph_runs_repo.finish(
                    graph_run_id=graph_run_id,
                    final_state="TIMEOUT",
                    outputs=None,
                    node_states={},
                    errors=["执行超时"],
                )
                await session.commit()
                logger.warning(f"Graph run {graph_run_id} 超时")

            except GraphRunNotFoundError:
                logger.error(f"Graph run {graph_run_id} 不存在，跳过执行")

            except Exception as e:
                # 所有异常都标记为 FAILED
                logger.exception(f"Graph run {graph_run_id} 执行失败")
                try:
                    await graph_runs_repo.finish(
                        graph_run_id=graph_run_id,
                        final_state="FAILED",
                        outputs=None,
                        node_states={},
                        errors=[f"执行异常: {type(e).__name__}: {e}"],
                    )
                    await session.commit()
                except Exception:
                    logger.exception("记录失败状态时出错")

    async def shutdown(self) -> None:
        """等待所有后台任务完成（API 关闭时调用）。"""
        if self._tasks:
            logger.info(f"等待 {len(self._tasks)} 个 Graph 任务完成...")
            await asyncio.gather(*self._tasks, return_exceptions=True)
            logger.info("所有 Graph 任务已完成")


__all__ = ["GraphWorker"]
