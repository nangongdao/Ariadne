"""Loop Worker —— 消费 Loop 任务并执行。

生命周期：API 创建 loop_runs 行 + 入队 → Worker 认领 → 租约 →
构造 engine → 执行（检查点恢复）→ 终态落库 → 释放租约。

崩溃接管的三层保障：
1. Redis 队列：任务未 ACK 时由其他 Worker XAUTOCLAIM 回收
2. Postgres 租约：loop_runs.lease_expires_at 过期后，补偿扫描
   （_reconcile，经 find_idle_runs）把该 Loop 重新入队 —— 即便 Redis
   队列条目丢失，也能从检查点续跑。双重执行由 begin_lease 的条件
   更新挡住：抢不到租约的 Worker 直接跳过
3. 入队失败兜底：API 创建/审批/恢复时入队失败（Redis 故障）的 Loop
   停在非终态但不在任何队列里，只有第 3 层能救 —— 按可配置间隔
   扫描"非终态 + 无有效租约 + 超时无进展"的行重新入队

engine 的正确性依赖 IO 全抽象（LLMClient/Clock/CheckpointStore），
本模块只负责装配：把 Postgres 检查点、Redis 预算计数、事件推送接进来。
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import signal
import socket
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import redis
import redis.asyncio as aioredis

from ariadne.auth.tenant import tenant_session
from ariadne.config import Settings, get_settings
from ariadne.loop_module.artifact import materialize
from ariadne.loop_module.checkpoint import Checkpoint
from ariadne.loop_module.engine import (
    LLMClient,
    LoopEngine,
    LoopOutcome,
)
from ariadne.loop_module.goal import AssertionKind, Goal
from ariadne.loop_module.parallel import ConcurrencyGate
from ariadne.loop_module.redis_counter import RedisCounter
from ariadne.loop_module.state_machine import LoopState, is_terminal
from ariadne.observability.metrics import (
    loop_cost_usd,
    loop_duration_seconds,
    loop_iterations,
    loop_terminal_total,
)
from ariadne.storage.postgres.engine import PostgresStore
from ariadne.storage.postgres.repositories.loop_checkpoint_repo import (
    LoopCheckpointRepository,
)
from ariadne.storage.postgres.repositories.loop_runs import (
    LoopRunConflictError,
    LoopRunNotFoundError,
    LoopRunRepository,
    goal_from_dict,
)
from ariadne.utils.logging import configure_logging, get_logger
from ariadne.worker.loop_queue import LoopQueue

logger = get_logger(__name__)

# 租约时长。Loop 单轮可能跑很久（等 LLM），用 goal 的墙钟上限兜底，
# 但至少给 60s 避免抖动。
MIN_LEASE_SECONDS = 60
_POLL_INTERVAL = 2.0
# 补偿扫描单租户单轮最多重新入队几个 Loop。
# 正常情况下命中数应为 0 或个位数；大量命中说明队列整体故障，
# 一次全量重灌只会把故障放大到下一轮。
_RECONCILE_BATCH_PER_PROJECT = 10


class _PgCheckpointStore:
    """CheckpointStore 的 Postgres 实现（Worker 装配用）。

    engine 每轮 save 一次、run 开头 latest 一次。每次调用开独立短
    会话：engine 是长任务（分钟级），持有一个连接跑完全程会占住
    连接池，且不 commit 的写入在崩溃时会全丢。
    """

    def __init__(self, pg: PostgresStore) -> None:
        self._pg = pg

    async def save(self, checkpoint: Checkpoint, *, project_id: uuid.UUID) -> None:
        async with tenant_session(self._pg, project_id) as session:
            await LoopCheckpointRepository(session).save(checkpoint, project_id=project_id)

    async def latest(self, loop_id: str, *, project_id: uuid.UUID) -> Checkpoint | None:
        async with tenant_session(self._pg, project_id) as session:
            return await LoopCheckpointRepository(session).latest(
                loop_id, project_id=project_id
            )


class _PostgresEventSink:
    """事件出口。M3 先记日志；SSE 推送在 API 层经 Redis pub/sub 实现。"""

    async def emit(self, event: Any) -> None:
        logger.debug("loop event", extra={"event": event.value})


class RedisEventSink:
    """经 Redis pub/sub 广播 Loop 状态变化。

    Worker 不直接连前端 —— 通过 Redis 频道 `loop:events:{loop_id}` 发布，
    API 的 SSE 端点订阅同一频道转发给浏览器。这样多 Worker 多 API 实例
    都能工作（M3-spec 6.5：SSE 从 Redis pub/sub 广播）。

    事件载荷：状态名 + 时间戳。前端据此渲染进化视图。

    用异步客户端（redis.asyncio）：emit 是协程，单连接天然保序；
    同步客户端 + 线程池会让并发 emit 乱序（进化视图依赖事件顺序）。
    """

    def __init__(self, redis_url: str, loop_id: str) -> None:
        self._redis_url = redis_url
        self._loop_id = loop_id
        self._redis: Any = None

    async def _client(self) -> Any:
        if self._redis is None:
            self._redis = aioredis.from_url(  # type: ignore[no-untyped-call]
                self._redis_url, encoding="utf-8", decode_responses=True
            )
        return self._redis

    async def emit(self, event: Any) -> None:
        import json as _json

        client = await self._client()
        payload = _json.dumps(
            {
                "event": event.value,
                "loop_id": self._loop_id,
                "ts": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
        )
        try:
            await client.publish(f"loop:events:{self._loop_id}", payload)
        except Exception as exc:
            logger.warning(
                "loop event publish failed",
                extra={"loop_id": self._loop_id, "error": str(exc)},
            )

    async def close(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None


def build_command_runner(settings: Settings) -> Any:
    """按 settings.sandbox 选命令执行器。

    沙箱可用 → SandboxRunner（真隔离）。
    不可用 → 看 fallback_to_restricted：
      True（默认）：返回 None 让调用方用受限子进程，记 warning。单机开发的常态。
      False：抛错拒绝启动 —— 显式关掉降级的部署是在要求"没有真隔离就
      不要执行不可信代码"，这种情况下静默降级比启动失败危险得多。

    profile 拼错等配置错误直接抛 ValueError，不降级：那不是环境不满足。

    allow_untrusted_code=True 时打 WARNING（docs/M3-spec 第 5 节承诺过这条
    日志）；且与"沙箱不可用"叠加时拒绝启动 —— 受限子进程隔离不了网络与
    syscall，声明要跑不可信代码却只有受限子进程，正是 R5 写明不可接受的组合。

    模块级函数而非 LoopWorker 方法：graph_module 的 Code 节点要用同一套决策
    （见 graph_module/runtime.py），而造 LoopWorker 需要 pg / 队列连接。两处
    各写一份的表现是改了一处安全检查、另一处仍是旧行为，且没有测试会发现。
    """
    from ariadne.sandbox_module.selector import build_sandbox

    untrusted = settings.sandbox.allow_untrusted_code
    if untrusted:
        logger.warning(
            "ARIADNE_SANDBOX_ALLOW_UNTRUSTED_CODE=true：允许执行不可信代码，"
            "仅在有真隔离后端时可接受",
            extra={"profile": settings.sandbox.profile},
        )

    sandbox = build_sandbox(settings.sandbox)
    if sandbox is not None:
        from ariadne.sandbox_module.base import SandboxProfile, SandboxRunner
        from ariadne.sandbox_module.selector import resolve_profile

        profile: SandboxProfile = resolve_profile(settings.sandbox.profile)
        logger.info(
            "命令执行使用沙箱执行器",
            extra={"backend": type(sandbox).__name__, "profile": profile.value},
        )
        return SandboxRunner(sandbox=sandbox, profile=profile)

    if not settings.sandbox.fallback_to_restricted:
        raise RuntimeError(
            "沙箱后端不可用且 ARIADNE_SANDBOX_FALLBACK_TO_RESTRICTED=false —— "
            "受限子进程防不住内核层逃逸，拒绝以无隔离状态执行命令断言"
        )

    if untrusted:
        raise RuntimeError(
            "ARIADNE_SANDBOX_ALLOW_UNTRUSTED_CODE=true 但沙箱后端不可用 —— "
            "受限子进程隔离不了网络与 syscall（见 R5），拒绝在无真隔离的前提下"
            "执行不可信代码。Windows 上没有可用的隔离后端，请置回 false"
        )

    logger.warning(
        "沙箱后端不可用，命令执行降级到受限子进程（无网络隔离、"
        "防不住内核层逃逸，勿用于不可信代码）",
        extra={"profile": settings.sandbox.profile},
    )
    return None


class LoopWorker:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        llm: LLMClient | None = None,
        pg_factory: Any | None = None,
        counter_factory: Any | None = None,
        queue: LoopQueue | None = None,
    ) -> None:
        """pg_factory：生产是 PostgresStore；测试注入内存/桩 store。
        counter_factory：生产是 RedisCounter；测试注入 InMemoryCounter。
        queue：生产是 LoopQueue；测试注入假队列（不连 Redis）。

        工厂返回值需满足 BudgetGuard 依赖的 BudgetCounter Protocol。
        """
        self._settings = settings or get_settings()
        self._llm = llm
        self._pg_factory = pg_factory or PostgresStore
        self._counter_factory = counter_factory

        def _default_counter(redis_url: str) -> Any:
            return RedisCounter(
                redis.Redis.from_url(redis_url, decode_responses=True)
            )

        self._counter_fn = counter_factory or _default_counter
        self._consumer = f"{socket.gethostname()}-{id(self)}"
        self._queue = queue or LoopQueue(self._settings.redis)
        self._running = False
        self._stats = {
            "claimed": 0,
            "completed": 0,
            "failed": 0,
            "skipped": 0,
            "reconciled": 0,
        }

    async def start(self) -> None:
        await self._queue.connect()
        self._running = True
        logger.info(
            "loop worker started",
            extra={"consumer": self._consumer, "has_llm": self._llm is not None},
        )
        reconcile_interval = self._settings.worker.reconcile_interval_seconds
        next_reconcile = 0.0
        while self._running:
            try:
                await self._poll()
                if reconcile_interval > 0 and time.monotonic() >= next_reconcile:
                    next_reconcile = time.monotonic() + reconcile_interval
                    await self._reconcile()
                await asyncio.sleep(_POLL_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "loop worker poll error",
                    extra={"error": str(exc)},
                    exc_info=True,
                )
                await asyncio.sleep(1.0)
        logger.info("loop worker stopped", extra=dict(self._stats))

    async def stop(self) -> None:
        self._running = False

    async def _poll(self) -> None:
        """一轮轮询：认领任务 → 并发处理（pipeline 语义）。

        并发而非串行是正确性要求，不是性能优化：`claim` 一次返回
        `_MAX_CLAIM`（3）个任务，单个 Loop 要跑几分钟，而未 ACK 消息 idle
        超过 `RECLAIM_MIN_IDLE_MS`（90s）就被其他 Worker 回收。串行处理时
        队头之后的任务干等着自己的回收期限到达 —— 它们还没获得 Postgres
        租约，接管者能干净地拿到租约并**并发执行同一个 Loop**。
        """
        claimed = await self._queue.claim(self._consumer)
        if not claimed:
            return

        pending: list[tuple[str, str, str]] = []
        for message_id, loop_id, project_id in claimed:
            self._stats["claimed"] += 1
            if not project_id:
                # 载荷缺 project_id（队列里的旧条目）：没有租户上下文就读不了
                # loop_runs。ACK 掉而非留 pending —— 留着会永占 pending 队头，
                # 把崩溃 Worker 的回收通道饿死，且每 90s 重试一次永远不会成功。
                self._stats["skipped"] += 1
                logger.warning(
                    "loop 任务缺 project_id，已丢弃；用 POST /loops/{id}/resume 重新入队",
                    extra={"loop_id": loop_id, "message_id": message_id},
                )
                await self._queue.ack(message_id)
                continue
            pending.append((message_id, loop_id, project_id))

        if not pending:
            return

        limit = max(1, self._settings.worker.loop_concurrency)
        gate = ConcurrencyGate(limit, floor=1)

        async def _one(message_id: str, loop_id: str, project_id: str) -> None:
            # 槽位在单项完成时立即释放（pipeline 语义），不等同批跑齐 ——
            # barrier 会让最慢的一项拖住其他项的租约续期。
            async with gate.slot():
                try:
                    await self._process(loop_id, project_id)
                    # 处理完成才 ACK。崩溃时不 ACK → 消息留 pending 被回收
                    await self._queue.ack(message_id)
                except Exception as exc:
                    self._stats["failed"] += 1
                    logger.error(
                        "loop processing failed",
                        extra={"loop_id": loop_id, "error": str(exc)},
                        exc_info=True,
                    )
                    # 不 ACK、不标记终态：租约过期后由他人接管，不重复执行

        # return_exceptions=True：单项异常不能取消同批其他项。_one 内部已经
        # 兜住了 Exception，这里兜的是它之外的（如 ack 自身失败）。
        await asyncio.gather(
            *(_one(*item) for item in pending), return_exceptions=True
        )

    async def _enqueue_with_retry(
        self, loop_id: str, project_id: str, *, attempts: int = 3
    ) -> bool:
        """带退避的入队。失败只返回 False，状态留给下一轮补偿扫描。"""
        for attempt in range(attempts):
            try:
                await self._queue.connect()
                await self._queue.ensure_group()
                await self._queue.enqueue(loop_id, project_id)
                return True
            except Exception as exc:
                logger.warning(
                    "loop reconcile enqueue attempt failed",
                    extra={
                        "loop_id": loop_id,
                        "attempt": attempt + 1,
                        "attempts": attempts,
                        "error": str(exc),
                    },
                )
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.5 * (2**attempt))
        return False

    async def _list_project_ids(self, pg: Any) -> list[Any]:
        """列出全部租户 id（共享辅助，见 worker/tenants.py）。

        projects 表**没有** RLS（它是租户边界的注册表，租户列表必须对
        认证后的服务可见），所以这一步不需要租户上下文。loop_runs 有
        RLS —— 逐租户会话扫描在 _reconcile 里做。
        """
        from ariadne.worker.tenants import list_project_ids

        return await list_project_ids(pg)

    async def _reconcile(self) -> None:
        """补偿扫描：把"已落库但不在队列里"的 Loop 重新入队。

        这是接管保障的第三层（此前只有前两层，而第二层从未被任何生产
        路径调用 —— R12 第五例）：
        1. Redis 队列：未 ACK 消息由其他 Worker 回收
        2. Postgres 租约：find_idle_runs 找过期租约 —— 但它此前没有调用方
        3. **本扫描**：两类队列里根本没有条目的 Loop ——
           a. 创建/审批/恢复时入队失败（Redis 短暂故障），Loop 永远停在
              非终态，XREAD 恢复不了从未写入的消息；
           b. 队列条目丢失（Redis flush / maxlen 截断）。

        双重执行由 begin_lease 的条件更新挡住：重复消息被认领后，只有一个
        Worker 能拿到租约，其余跳过（租约过期阈值远大于扫描间隔）。

        RLS：loop_runs 在生产库（app 角色）必须设租户变量才可见，所以
        先列 project_id，再逐租户会话扫描。入队失败不打状态改动 ——
        下一轮扫描自然会重试。
        """
        pg = self._pg_factory(self._settings.postgres)
        try:
            project_ids = await self._list_project_ids(pg)
            idle = self._settings.worker.reconcile_idle_seconds
            for project_id in project_ids:
                try:
                    async with tenant_session(pg, project_id) as session:
                        runs = await LoopRunRepository(session).find_idle_runs(
                            project_id=project_id,
                            limit=_RECONCILE_BATCH_PER_PROJECT,
                            idle_seconds=idle,
                        )
                except Exception as exc:
                    logger.warning(
                        "loop reconcile scan failed",
                        extra={"project_id": str(project_id), "error": str(exc)},
                    )
                    continue
                for run in runs:
                    delivered = await self._enqueue_with_retry(
                        str(run.id), str(project_id)
                    )
                    if delivered:
                        self._stats["reconciled"] += 1
                        logger.info(
                            "补偿扫描重新入队卡住的 Loop",
                            extra={
                                "loop_id": str(run.id),
                                "state": run.state,
                                "updated_at": run.updated_at.isoformat()
                                if run.updated_at
                                else None,
                            },
                        )
        finally:
            await pg.close()

    async def _process(self, loop_id: str, project_id: str) -> None:
        """处理单个 Loop：装配 engine 并执行到终态。

        project_id 来自队列载荷，是不可信声明：拿它设 RLS 变量后所有查询
        仍走 RLS，声明错了只会读不到行（跳过），不会跨租户读到数据。
        """
        pg = self._pg_factory(self._settings.postgres)
        try:
            loop_uuid = uuid.UUID(loop_id)
            claimed_project = uuid.UUID(project_id)
            async with tenant_session(pg, claimed_project) as session:
                repo = LoopRunRepository(session)
                try:
                    run = await repo.by_id(loop_uuid)
                except LoopRunNotFoundError:
                    # 队列里的残留任务（行已删）：跳过
                    self._stats["skipped"] += 1
                    return
                if is_terminal(LoopState(run.state)):
                    self._stats["skipped"] += 1
                    return

                goal = goal_from_dict(run.goal)
                duration = self._lease_duration(goal)
                try:
                    # 租约归属由 begin_lease 在 UPDATE ... WHERE 里判定。
                    # 这里不再"先读 lease_expires_at 再决定要不要抢"——那是
                    # check-then-act：两个 Worker 可以同时读到过期租约、
                    # 同时通过检查，然后并发执行同一个 Loop。
                    await repo.begin_lease(
                        loop_id=run.id,
                        worker_id=self._consumer,
                        duration_seconds=duration,
                    )
                except LoopRunConflictError:
                    # 租约被他人持有且未过期：本 Worker 不抢
                    self._stats["skipped"] += 1
                    return

            # engine 运行期间不持有会话：检查点/终态各自开短事务
            workspace = self._prepare_workspace(loop_uuid, goal)
            engine = await self._build_engine(
                loop_uuid, goal, pg, run.project_id, workspace
            )
            lease_task = asyncio.create_task(
                self._lease_loop(loop_uuid, duration, pg, run.project_id)
            )
            loop_start = time.monotonic()
            try:
                outcome = await engine.run()
            finally:
                lease_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await lease_task

            elapsed = time.monotonic() - loop_start
            await self._finalize(loop_uuid, outcome, pg, run.project_id)
            # 终态才清理工作目录：非终态（如 HUMAN_PENDING）要留给接管的
            # Worker 复用已落盘的产出物，删了就等于回退到初始状态
            self._cleanup_workspace(loop_uuid, workspace, outcome.final_state)
            # --- Prometheus 指标 ---
            project_label = str(run.project_id)
            mode_label = goal.mode  # LoopMode is a Literal string
            loop_iterations.labels(
                project=project_label, mode=mode_label, final_state=outcome.final_state.value
            ).observe(outcome.iterations)
            loop_duration_seconds.labels(
                project=project_label, mode=mode_label
            ).observe(elapsed)
            loop_cost_usd.labels(
                project=project_label, mode=mode_label, model=self._settings.llm.model
            ).observe(float(outcome.usage.cost_usd))
            loop_terminal_total.labels(
                project=project_label, final_state=outcome.final_state.value
            ).inc()
            self._stats["completed"] += 1
        finally:
            await pg.close()

    def _lease_duration(self, goal: Goal) -> int:
        return max(MIN_LEASE_SECONDS, goal.budget.max_wall_clock_seconds)

    def _needs_workspace(self, goal: Goal) -> bool:
        """只有 COMMAND 断言需要磁盘上的文件。

        其余断言类型（REGEX/SCHEMA/METRIC）是 output 字符串的纯函数，
        给它们建目录纯属多余 IO。工具调用的目录由 WorkspaceToolExecutor
        按需创建（见 _tool_dir），不在此预建。
        """
        return any(a.kind is AssertionKind.COMMAND for a in goal.assertions)

    def _workspace_base(self) -> Path:
        """工作目录的根（_prepare_workspace 与工具执行器共用同一约定）。"""
        root = self._settings.sandbox.workspace_root
        return Path(root) if root else Path(tempfile.gettempdir()) / "ariadne-loops"

    def _tool_dir(self, loop_id: uuid.UUID) -> Path:
        """工具执行器的惰性工作目录（模型请求工具时才真正创建）。"""
        return self._workspace_base() / str(loop_id)

    def _prepare_workspace(self, loop_id: uuid.UUID, goal: Goal) -> Path | None:
        """为带 COMMAND 断言的 Loop 准备工作目录，写入种子文件。

        **这是 COMMAND 断言在生产路径上可用的前提。** 此前 LoopConfig 从
        不传 artifact_path，于是 `_validate` 拿到 sandbox_available=False，
        goal_validation 对任何 COMMAND 断言报 error —— 代码生成场景（M3
        标杆场景、"信号最硬的那类断言"）在生产上会被直接判 REJECTED。

        目录按 loop_id 确定性命名：同机接管的 Worker 能复用上一个 Worker
        已落盘的产出物。跨机接管拿不到（本地文件系统的固有限制），此时
        种子文件会被重新写入，Loop 从初始状态续跑 —— 检查点里的预算与轮次
        不受影响，不会重复计费。
        """
        if not self._needs_workspace(goal):
            return None

        base = self._workspace_base()
        workspace = base / str(loop_id)
        try:
            workspace.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error(
                "无法创建 Loop 工作目录，COMMAND 断言将不可用",
                extra={"loop_id": str(loop_id), "path": str(workspace), "error": str(exc)},
            )
            return None

        if goal.workspace:
            report = materialize(goal.workspace, workspace)
            if not report.ok:
                # 种子文件写不进去就别开跑：COMMAND 断言会验证一个不完整的
                # 工作目录，得出的结论与目标无关
                logger.error(
                    "种子文件落盘失败",
                    extra={"loop_id": str(loop_id), "error": report.error},
                )
                raise RuntimeError(f"Loop {loop_id} 种子文件落盘失败: {report.error}")
            logger.info(
                "Loop 工作目录就绪",
                extra={"loop_id": str(loop_id), "files": list(report.written)},
            )
        return workspace

    def _cleanup_workspace(
        self,
        loop_uuid: uuid.UUID,
        workspace: Path | None,
        final_state: LoopState,
    ) -> None:
        """终态后删除工作目录。非终态保留供接管。"""
        if not is_terminal(final_state):
            return
        if workspace is not None:
            # COMMAND 断言的预建目录 == 工具执行器的目录，删一次即可
            shutil.rmtree(workspace, ignore_errors=True)
            return
        # 无 COMMAND 断言的 Loop 没有预建工作目录，但工具调用可能创建过
        shutil.rmtree(self._tool_dir(loop_uuid), ignore_errors=True)

    async def _build_engine(
        self,
        loop_uuid: uuid.UUID,
        goal: Goal,
        pg: PostgresStore,
        project_id: uuid.UUID,
        workspace: Path | None = None,
    ) -> LoopEngine:
        """装配 engine —— 委托给 assembly 模块。"""
        if self._llm is None:
            raise RuntimeError(
                "LoopWorker 未注入 LLMClient —— 生产装配见 cli.run_loop_worker"
            )
        from ariadne.worker.assembly import build_loop_engine

        return await build_loop_engine(
            loop_id=loop_uuid,
            goal=goal,
            pg=pg,
            project_id=project_id,
            settings=self._settings,
            llm_client=self._llm,
            counter_fn=self._counter_fn,
            workspace=workspace,
            workspace_base=self._workspace_base(),
        )

    async def _lease_loop(
        self,
        loop_uuid: uuid.UUID,
        duration: int,
        pg: PostgresStore,
        project_id: uuid.UUID,
    ) -> None:
        """定期续租，防止长 Loop 租约过期被误接管。"""
        interval = max(duration / 2, 5)
        while self._running:
            await asyncio.sleep(interval)
            try:
                async with tenant_session(pg, project_id) as session:
                    await LoopRunRepository(session).extend_lease(
                        loop_id=loop_uuid,
                        worker_id=self._consumer,
                        duration_seconds=duration,
                    )
            except Exception as exc:
                logger.warning(
                    "lease extension failed",
                    extra={"loop_id": str(loop_uuid), "error": str(exc)},
                )
                break

    async def _finalize(
        self,
        loop_uuid: uuid.UUID,
        outcome: LoopOutcome,
        pg: PostgresStore,
        project_id: uuid.UUID,
    ) -> None:
        """终态落库：state/final_state/iteration/cumulative + 释放租约。"""
        try:
            async with tenant_session(pg, project_id) as session:
                repo = LoopRunRepository(session)
                await repo.update_metrics(
                    loop_id=loop_uuid,
                    iteration=outcome.iterations,
                    tokens=outcome.usage.total_tokens,
                    cost_usd=float(outcome.usage.cost_usd),
                )
                await repo.finish(
                    loop_id=loop_uuid,
                    final_state=outcome.final_state,
                    error="",
                    worker_id=self._consumer,
                )
        except Exception as exc:
            logger.error(
                "finalize failed",
                extra={"loop_id": str(loop_uuid), "error": str(exc)},
            )
        logger.info(
            "loop completed",
            extra={
                "loop_id": str(loop_uuid),
                "final_state": outcome.final_state.value,
                "iterations": outcome.iterations,
            },
        )

    async def close(self) -> None:
        await self._queue.close()


async def run_loop_worker(llm: LLMClient | None = None) -> None:
    """CLI 入口：ariadne-loop-worker。"""
    settings = get_settings()
    configure_logging(settings.log_level)
    worker = LoopWorker(settings, llm=llm)

    loop = asyncio.get_running_loop()
    task = asyncio.create_task(worker.start())

    def _shutdown() -> None:
        logger.info("shutdown signal received")
        task_stop = asyncio.create_task(worker.stop())
        task_stop.add_done_callback(lambda _: None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows 不支持 add_signal_handler
            loop.add_signal_handler(sig, _shutdown)

    try:
        await task
    finally:
        await worker.close()


__all__ = ["LoopWorker", "build_command_runner", "run_loop_worker"]