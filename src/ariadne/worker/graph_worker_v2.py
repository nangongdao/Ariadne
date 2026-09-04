"""GraphWorker（阶段 2）：Redis 队列 + 租约机制 + 补偿扫描。

从 MVP 的后台任务池升级为独立 Worker 进程：
- 从 Redis Streams 消费任务（GraphQueue）
- 使用 Postgres 租约防止多 Worker 重复执行
- 定期扫描过期租约并重新入队（补偿扫描）
- 支持优雅关闭（等待当前任务完成）

参考 LoopWorker 架构，但简化了状态机（Graph 不需要迭代状态）。
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING, Any

from ariadne.auth.tenant import tenant_session
from ariadne.graph_module.checkpointing import (
    GraphRunCheckpointSaver,
    load_checkpoint,
    load_checkpoint_outputs,
)
from ariadne.graph_module.executor import GraphExecutor, NodeExecutor
from ariadne.graph_module.models import NodeKind, WorkflowGraph
from ariadne.graph_module.runtime import build_node_executors_from_settings
from ariadne.graph_module.serialize import spec_to_graph
from ariadne.runtime_module.llm.resolver import resolve_project_llm
from ariadne.storage.postgres.repositories.graph_runs import (
    GraphRunNotFoundError,
    GraphRunRepository,
)
from ariadne.storage.postgres.repositories.graphs import GraphRepository
from ariadne.worker.graph_queue import GraphQueue
from ariadne.worker.tenants import list_project_ids

if TYPE_CHECKING:
    from ariadne.config import Settings
    from ariadne.storage.postgres.engine import PostgresStore

logger = logging.getLogger(__name__)


class GraphWorkerV2:
    """Graph 异步执行器（阶段 2：Redis 队列 + 租约机制）。

    与 MVP 的区别：
    - 从 Redis Streams 消费任务，不依赖 API 进程
    - 使用 Postgres 租约防止多 Worker 重复执行
    - 补偿扫描回收过期租约
    - 支持独立进程部署和横向扩展
    """

    def __init__(
        self,
        pg_factory: PostgresStore,
        settings: Settings,
        queue: GraphQueue,
    ) -> None:
        self._pg_factory = pg_factory
        self._settings = settings
        self._queue = queue
        self._worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self._shutdown_event = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def worker_id(self) -> str:
        return self._worker_id

    async def run(self) -> None:
        """主循环：消费队列 + 补偿扫描。"""
        await self._queue.ensure_group()
        logger.info(f"GraphWorker {self._worker_id} 启动")

        # 启动补偿扫描任务
        reconcile_task = asyncio.create_task(self._reconcile_loop())
        self._tasks.add(reconcile_task)

        try:
            while not self._shutdown_event.is_set():
                try:
                    # 从队列认领任务
                    jobs = await self._queue.claim(
                        consumer=self._worker_id,
                        count=3,  # 一次最多拿 3 个任务
                        block_ms=1000,
                    )

                    if jobs:
                        logger.info(f"认领 {len(jobs)} 个 Graph 任务")

                    for message_id, graph_run_id, project_id in jobs:
                        # 并发执行（但受 Postgres 租约限制，同一任务不会重复执行）
                        task = asyncio.create_task(
                            self._execute_with_lease(
                                message_id,
                                uuid.UUID(graph_run_id),
                                uuid.UUID(project_id),
                            )
                        )
                        self._tasks.add(task)
                        task.add_done_callback(self._tasks.discard)

                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("消费队列时出错，继续重试")
                    await asyncio.sleep(5)

        finally:
            logger.info("等待所有 Graph 任务完成...")
            await asyncio.gather(*self._tasks, return_exceptions=True)
            logger.info("GraphWorker 已关闭")

    async def _execute_with_lease(
        self,
        message_id: str,
        graph_run_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> None:
        """带租约控制的执行入口。"""
        async with tenant_session(self._pg_factory, project_id) as session:
            repo = GraphRunRepository(session)

            # 尝试认领租约
            success = await repo.begin_lease(
                graph_run_id,
                self._worker_id,
                self._settings.worker.graph_lease_duration_s,
            )
            await session.commit()

            if not success:
                # 租约被其他 Worker 抢占（或任务已完成），跳过
                logger.info(f"Graph run {graph_run_id} 租约认领失败，跳过")
                await self._queue.ack(message_id)
                return

            logger.info(f"Graph run {graph_run_id} 租约认领成功，开始执行")

            try:
                # 启动租约续期任务
                lease_task = asyncio.create_task(
                    self._lease_extender(graph_run_id, project_id)
                )

                try:
                    # 执行图
                    await self._execute(graph_run_id, project_id)
                finally:
                    # 停止租约续期
                    lease_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await lease_task

                # ACK 队列消息
                await self._queue.ack(message_id)
                logger.info(f"Graph run {graph_run_id} 执行完成并 ACK")

            except Exception:
                logger.exception(f"Graph run {graph_run_id} 执行失败")
                # 不 ACK，让 Redis 超时后由其他 Worker 回收
                # Postgres 租约已过期，其他 Worker 可以重新认领

    async def _lease_extender(
        self, graph_run_id: uuid.UUID, project_id: uuid.UUID
    ) -> None:
        """定期续约任务（后台运行）。"""
        while True:
            await asyncio.sleep(
                self._settings.worker.graph_lease_extend_interval_s
            )
            try:
                async with tenant_session(self._pg_factory, project_id) as session:
                    repo = GraphRunRepository(session)
                    success = await repo.extend_lease(
                        graph_run_id,
                        self._worker_id,
                        self._settings.worker.graph_lease_duration_s,
                    )
                    await session.commit()
                    if success:
                        logger.debug(f"Graph run {graph_run_id} 租约续期成功")
                    else:
                        logger.warning(
                            f"Graph run {graph_run_id} 租约续期失败，可能已被接管"
                        )
                        break
            except Exception:
                logger.exception(f"Graph run {graph_run_id} 租约续期时出错")

    async def _execute(
        self,
        graph_run_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> None:
        """执行单个 graph run（核心逻辑，从 MVP 移植）。"""
        async with tenant_session(self._pg_factory, project_id) as session:
            graph_runs_repo = GraphRunRepository(session)
            graphs_repo = GraphRepository(session)

            try:
                # 1. 读取 graph_run + graph 定义
                run = await graph_runs_repo.by_id(graph_run_id)
                graph_row = await graphs_repo.get(
                    project_id=project_id, graph_id=run.graph_id
                )

                # 2. 检查取消标志（阶段 3）
                if await graph_runs_repo.is_cancelled(graph_run_id):
                    logger.info(f"Graph run {graph_run_id} 已被取消，退出执行")
                    await graph_runs_repo.finish(
                        graph_run_id=graph_run_id,
                        final_state="CANCELLED",
                        outputs=None,
                        node_states={},
                        errors=["用户取消执行"],
                    )
                    await session.commit()
                    return

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

                # 阶段 3-2：加载检查点（如果存在）
                checkpoint = await load_checkpoint(graph_run_id, graph_runs_repo)
                checkpoint_outputs = await load_checkpoint_outputs(
                    graph_run_id, graph_runs_repo
                )
                # 有状态检查点但没有可读的输出（例如滚动迁移期间或旧格式）
                # 时，显式传空 dict，让执行器重新计算节点；不能退回只按状态
                # 跳过，否则下游会收到空输入。
                if checkpoint and checkpoint_outputs is None:
                    checkpoint_outputs = {}
                completed_nodes = None
                if checkpoint:
                    completed_nodes = {
                        node_id
                        for node_id, state in checkpoint.items()
                        if state == "completed"
                    }
                    if completed_nodes:
                        logger.info(
                            f"Graph run {graph_run_id} 从检查点恢复，"
                            f"跳过 {len(completed_nodes)} 个已完成节点"
                        )

                # 阶段 3-2：创建检查点保存器
                checkpoint_saver = GraphRunCheckpointSaver(
                    graph_run_id,
                    graph_runs_repo,
                    initial_states=checkpoint,
                    initial_outputs=checkpoint_outputs,
                )

                # 4. 执行 GraphExecutor.run()（带取消检查和检查点）
                # 阶段 3-3：SSE 事件发布（节点进度 + 起止事件）
                from ariadne.worker.graph_events import GraphEventPublisher

                publisher = GraphEventPublisher(
                    self._settings.redis.url, str(graph_run_id)
                )
                run_started_at = time.monotonic()
                final_state = "FAILED"  # 异常路径默认为 FAILED，finally 兜底
                try:
                    await publisher.publish_run_started()
                    exec_result = await self._run_with_cancellation_check(
                        graph_run_id,
                        graph,
                        run.inputs,
                        node_executors,
                        graph_runs_repo,
                        checkpoint_saver,
                        completed_nodes,
                        checkpoint_outputs,
                        publisher,
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
                    # 阶段 3-3：终态事件 + 指标
                    try:
                        await publisher.publish_run_finished(final_state)
                    except Exception:
                        logger.exception("发布 graph 终态事件失败")
                    await publisher.close()
                    self._record_metrics(
                        project_id,
                        str(graph_run_id),
                        final_state,
                        exec_result if "exec_result" in locals() else None,
                        time.monotonic() - run_started_at,
                    )
                    # 清理 LLM client
                    if resolved_llm is not None:
                        close = getattr(resolved_llm, "aclose", None)
                        if close is not None:
                            await close()

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

    async def _run_with_cancellation_check(
        self,
        graph_run_id: uuid.UUID,
        graph: WorkflowGraph,
        inputs: dict[str, Any],
        node_executors: dict[NodeKind, NodeExecutor],
        repo: GraphRunRepository,
        checkpoint_saver: Any,
        completed_nodes: set[str] | None,
        completed_outputs: dict[str, dict[str, Any]] | None,
        publisher: Any,
    ) -> Any:
        """执行图并在执行前检查取消标志（阶段 3）。

        阶段 3-2 新增：支持检查点保存和恢复。
        阶段 3-3 新增：节点事件发布（SSE 进度推送）。
        """
        # 执行前再次检查取消标志
        if await repo.is_cancelled(graph_run_id):
            logger.info(f"Graph run {graph_run_id} 执行前检测到取消标志")
            raise RuntimeError("执行已取消")

        # 执行图（阶段 3-2：传入检查点保存器和已完成节点）
        executor = GraphExecutor()
        return await executor.run(
            graph,
            inputs=inputs,
            node_executors=node_executors,
            checkpoint_saver=checkpoint_saver,
            completed_nodes=completed_nodes,
            completed_outputs=completed_outputs,
            node_event_callback=publisher,
        )

    @staticmethod
    def _record_metrics(
        project_id: uuid.UUID,
        graph_run_id: str,
        final_state: str,
        exec_result: Any,
        duration_s: float,
    ) -> None:
        """Graph 执行指标（阶段 3-3）。

        尽力而为：Prometheus 未初始化时缺指标只记日志，不阻塞执行。
        """
        try:
            from ariadne.observability.metrics import (
                graph_duration_seconds,
                graph_node_duration_seconds,
                graph_terminal_total,
            )

            project = str(project_id)
            graph_terminal_total.labels(
                project=project, final_state=final_state
            ).inc()
            graph_duration_seconds.labels(project=project).observe(duration_s)

            if exec_result is not None:
                node_states = exec_result.node_states or {}
                terminal_count = sum(
                    1
                    for s in node_states.values()
                    if s.value in ("completed", "failed", "skipped")
                )
                if terminal_count:
                    graph_node_duration_seconds.labels(
                        project=project, node_type="all"
                    ).observe(duration_s / terminal_count)
        except Exception as exc:
            logger.warning(
                "graph metrics record failed",
                extra={"graph_run_id": graph_run_id, "error": str(exc)},
            )

    async def _reconcile_loop(self) -> None:
        """补偿扫描循环：定期回收过期租约并重新入队。"""
        interval = self._settings.worker.reconcile_interval_seconds
        while not self._shutdown_event.is_set():
            try:
                await asyncio.sleep(interval)
                await self._reconcile()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("补偿扫描时出错")

    async def _reconcile(self) -> None:
        """扫描并原子回收过期租约，直接在当前 Worker 执行。

        Graph runs 受 RLS 保护，后台扫描必须逐项目建立租户会话；直接用
        普通 session 会在生产的 ``ariadne_app`` 角色下静默看不到任何行。
        ``reclaim_expired_lease`` 的条件 UPDATE 负责并发仲裁，避免扫描器
        只是重复入队却没有真正接管任务。
        """
        try:
            project_ids = await list_project_ids(self._pg_factory)
        except Exception:
            logger.exception("枚举 Graph 租户时出错")
            return

        claimed: list[tuple[uuid.UUID, uuid.UUID]] = []
        for project_id in project_ids:
            project_claimed: list[tuple[uuid.UUID, uuid.UUID]] = []
            try:
                async with tenant_session(
                    self._pg_factory, project_id
                ) as session:
                    repo = GraphRunRepository(session)
                    expired = await repo.find_expired_leases(limit=50)
                    for run in expired:
                        if await repo.reclaim_expired_lease(
                            run.id,
                            self._worker_id,
                            self._settings.worker.graph_lease_duration_s,
                        ):
                            project_claimed.append((run.id, project_id))
            except Exception:
                logger.exception(
                    "回收 Graph 过期租约时出错",
                    extra={"project_id": str(project_id)},
                )
                continue
            claimed.extend(project_claimed)

        if not claimed:
            return

        logger.warning("已回收 %d 个 Graph 过期租约", len(claimed))
        for graph_run_id, project_id in claimed:
            task = asyncio.create_task(
                self._execute_reclaimed(graph_run_id, project_id)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _execute_reclaimed(
        self,
        graph_run_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> None:
        """执行已由补偿扫描原子接管的任务（不再重复 begin_lease）。"""
        lease_task = asyncio.create_task(
            self._lease_extender(graph_run_id, project_id)
        )
        try:
            await self._execute(graph_run_id, project_id)
        except Exception:
            # _execute 通常会把业务异常落为 FAILED；这里兜住连接/编程异常，
            # 让补偿任务不会产生未取出的 asyncio exception。
            logger.exception("回收后的 Graph run 执行异常")
        finally:
            lease_task.cancel()
            with suppress(asyncio.CancelledError):
                await lease_task

    async def shutdown(self) -> None:
        """优雅关闭：停止接收新任务，等待当前任务完成。"""
        logger.info("收到关闭信号，停止接收新任务...")
        self._shutdown_event.set()


@asynccontextmanager
async def create_graph_worker_v2(
    pg_factory: PostgresStore,
    settings: Settings,
    queue: GraphQueue,
) -> AsyncIterator[GraphWorkerV2]:
    """创建 GraphWorkerV2 实例（上下文管理器）。"""
    worker = GraphWorkerV2(pg_factory, settings, queue)
    try:
        yield worker
    finally:
        await worker.shutdown()


__all__ = ["GraphWorkerV2", "create_graph_worker_v2"]
