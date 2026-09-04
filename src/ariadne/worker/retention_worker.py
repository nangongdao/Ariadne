"""Retention Worker —— 消费 deletion_jobs 表，执行 GDPR 级联删除。

存在的理由：`DELETE /v1/projects/{id}/data` 之前只往 deletion_jobs 插一行
`status="pending"` 就返 202，没有任何进程消费这些行。端点对外承诺"删除请求
已受理"，实际数据一条没删 —— 假成功的合规端点。本 Worker 补上消费侧。

为什么用表轮询而不是 Redis 队列（另两个 Worker 的做法）：
- 删除任务量极小（人工触发，日均个位数），队列的吞吐优势用不上
- 任务状态本来就必须落库（M6 §4.3 要求可查进度），用表当队列少一处状态源
- Redis 丢消息意味着删除请求静默消失，而这里丢不起

崩溃安全：认领时把 status 从 pending 改成 postgres_done 之前不提交，
崩溃后行仍是 pending 会被重新认领。删除操作本身幂等（DELETE WHERE 重跑
删 0 行），重放安全。级联中途崩溃停在 postgres_done 的任务也会被重新
认领并从断点继续 —— 级联的每一步都幂等，从哪一步重跑都安全。

多副本安全（P1-8）：认领是"FOR UPDATE SKIP LOCKED + 租约条件"的原子操作，
拿到租约的副本执行级联，其余副本跳过；租约过期后任务可被重新认领。

ClickHouse mutation 是异步的，所以 Worker 有两条路径：
1. 新 pending 任务 → 执行级联 → 落 clickhouse_mutation_submitted / s3_done
2. 已提交 mutation 的任务 → 轮询是否完成 → 落 completed
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import socket
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select, update

from ariadne.auth.tenant import tenant_session
from ariadne.config import Settings, get_settings
from ariadne.observability.metrics import gdpr_deletion_total
from ariadne.storage.postgres.retention_models import DeletionJobRow
from ariadne.utils.logging import configure_logging, get_logger
from ariadne.worker.tenants import list_project_ids

logger = get_logger(__name__)

# 删除任务是人工触发的低频操作，轮询间隔可以放宽
_POLL_INTERVAL = 10.0
# 单轮每个项目最多处理几个任务 —— 级联删除是重操作，不要一次吃太多
_BATCH = 5
# 认领租约时长。级联含 S3 与 ClickHouse 交互，给足余量；
# 过期后任务被重新认领，幂等重放无害。
_LEASE_SECONDS = 600

_PENDING = "pending"
_POSTGRES_DONE = "postgres_done"
_MUTATION_SUBMITTED = "clickhouse_mutation_submitted"
_S3_DONE = "s3_done"
_COMPLETED = "completed"
_FAILED = "failed"

# 已提交 mutation、等待确认完成的中间态
_AWAITING_MUTATION = (_MUTATION_SUBMITTED, _S3_DONE)
# 需要执行/续跑级联的状态（认领 + 租约）。postgres_done 说明上一个
# Worker 崩在级联中途，从断点续跑而不是永远卡住。
_CASCADE_STATUSES = (_PENDING, _POSTGRES_DONE)


class RetentionWorker:
    """轮询 deletion_jobs 并执行级联删除。

    三个 deleter 都可注入：测试传 Fake，生产走 _build_deleters。
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        pg: Any | None = None,
        postgres_deleter: Any | None = None,
        clickhouse_deleter: Any | None = None,
        object_store_deleter: Any | None = None,
        poll_interval: float = _POLL_INTERVAL,
    ) -> None:
        self._settings = settings or get_settings()
        self._pg = pg
        self._pg_deleter = postgres_deleter
        self._ch_deleter = clickhouse_deleter
        self._os_deleter = object_store_deleter
        self._poll_interval = poll_interval
        self._built = False
        self._running = False
        self._worker_id = f"{socket.gethostname()}-{id(self)}"
        self._stats = {"claimed": 0, "completed": 0, "failed": 0, "awaiting": 0}

    def _ensure_deleters(self) -> None:
        """惰性装配：start() 与 run_once() 都要能独立使用。

        此前装配只在 start() 里做，直接调 run_once()（单次处理入口，
        运维排障与验收都用它）会在 assert self._pg 上炸掉。
        全部依赖已注入时本方法是 no-op（测试注入 Fake 不被覆盖）。
        """
        if self._built:
            return
        self._build_deleters()
        self._built = True

    def _build_deleters(self) -> None:
        """按 settings 装配三个真实 deleter（生产路径）。"""
        from ariadne.storage.clickhouse import ClickHouseStore
        from ariadne.storage.deleters import (
            ClickHouseMutationDeleter,
            ObjectStorePrefixDeleter,
            PostgresCascadeDeleter,
        )
        from ariadne.storage.objectstore import build_store
        from ariadne.storage.postgres.engine import PostgresStore

        if self._pg is None:
            self._pg = PostgresStore(self._settings.postgres)
        if self._pg_deleter is None:
            self._pg_deleter = PostgresCascadeDeleter(self._pg)
        if self._ch_deleter is None:
            self._ch_deleter = ClickHouseMutationDeleter(
                ClickHouseStore(self._settings.clickhouse)
            )
        if self._os_deleter is None:
            self._os_deleter = ObjectStorePrefixDeleter(
                build_store(self._settings.payload)
            )

    async def start(self) -> None:
        self._ensure_deleters()
        self._running = True
        logger.info("retention worker started")

        while self._running:
            try:
                processed = await self.run_once()
                if not processed:
                    await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "retention worker loop error",
                    extra={"error": str(exc)},
                    exc_info=True,
                )
                await asyncio.sleep(self._poll_interval)

        logger.info("retention worker stopped", extra=dict(self._stats))

    async def stop(self) -> None:
        self._running = False

    async def run_once(self) -> int:
        """处理一轮：新 pending 任务 + 确认已提交的 mutation。

        返回本轮处理的任务数（0 表示无事可做，调用方可以睡）。
        """
        self._ensure_deleters()
        pending = await self._claim_cascade_jobs()
        for job_id, project_id, subject_id in pending:
            await self._execute_cascade(job_id, project_id, subject_id)
            self._stats["claimed"] += 1

        awaiting = await self._fetch_awaiting_jobs()
        for job_id, project_id in awaiting:
            await self._check_mutation(job_id, project_id)

        return len(pending) + len(awaiting)

    async def _claim_cascade_jobs(self) -> list[tuple[UUID, UUID, str]]:
        """原子认领需要执行级联的任务（pending / postgres_done）。

        RLS：生产库 deletion_jobs 有行级安全，未设租户变量的查询**看不到
        任何行**（compose 里 API/Worker 都跑在非 owner 的 ariadne_app 角色
        下 —— 这不是理论问题，是默认部署就会踩的坑）。所以逐项目建立
        tenant_session 查询。

        认领 = 行锁（FOR UPDATE SKIP LOCKED，SQLite 降级为无锁）+ 条件
        UPDATE 写租约。拿到租约的副本执行级联；租约过期（Worker 崩溃）
        的任务可被其他副本重新认领 —— 级联幂等，重放安全。
        """
        assert self._pg is not None
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=_LEASE_SECONDS)
        claimed: list[tuple[UUID, UUID, str]] = []

        for project_id in await list_project_ids(self._pg):
            try:
                async with tenant_session(self._pg, project_id) as session:
                    stmt = (
                        select(
                            DeletionJobRow.id,
                            DeletionJobRow.project_id,
                            DeletionJobRow.subject_id,
                        )
                        .where(
                            DeletionJobRow.status.in_(_CASCADE_STATUSES),
                            DeletionJobRow.project_id == project_id,
                            or_(
                                DeletionJobRow.lease_expires_at.is_(None),
                                DeletionJobRow.lease_expires_at < now,
                            ),
                        )
                        .order_by(DeletionJobRow.created_at)
                        .limit(_BATCH)
                        .with_for_update(skip_locked=True)
                    )
                    rows = (await session.execute(stmt)).all()
                    if not rows:
                        continue
                    await session.execute(
                        update(DeletionJobRow)
                        .where(
                            DeletionJobRow.id.in_([r[0] for r in rows]),
                            # 条件更新：只认领租约仍可获取的行。SELECT 到
                            # UPDATE 之间租约不会变（行锁 + 同事务），这是
                            # 双保险而非唯一防线。
                            or_(
                                DeletionJobRow.lease_expires_at.is_(None),
                                DeletionJobRow.lease_expires_at < now,
                            ),
                        )
                        .values(
                            claimed_by=self._worker_id,
                            lease_expires_at=lease_until,
                        )
                    )
                    claimed.extend((r[0], r[1], r[2]) for r in rows)
            except Exception as exc:
                logger.warning(
                    "deletion job claim failed",
                    extra={"project_id": str(project_id), "error": str(exc)},
                )
                continue
        return claimed

    async def _fetch_awaiting_jobs(
        self,
    ) -> list[tuple[UUID, UUID]]:
        """取等待 mutation 完成的任务。只读不认领 —— 完成检查幂等。"""
        assert self._pg is not None
        awaiting: list[tuple[UUID, UUID]] = []
        for project_id in await list_project_ids(self._pg):
            try:
                async with tenant_session(self._pg, project_id) as session:
                    stmt = (
                        select(DeletionJobRow.id, DeletionJobRow.project_id)
                        .where(
                            DeletionJobRow.status.in_(_AWAITING_MUTATION),
                            DeletionJobRow.project_id == project_id,
                        )
                        .order_by(DeletionJobRow.created_at)
                        .limit(_BATCH)
                    )
                    rows = (await session.execute(stmt)).all()
                    awaiting.extend((r[0], r[1]) for r in rows)
            except Exception as exc:
                logger.warning(
                    "deletion job fetch failed",
                    extra={"project_id": str(project_id), "error": str(exc)},
                )
                continue
        return awaiting

    async def _execute_cascade(
        self, job_id: UUID, project_id: UUID, subject_id: str
    ) -> None:
        """三处存储级联删除。任一步失败即标 failed 并记录原因。

        从 postgres_done 续跑时前面的步骤重放：DELETE WHERE 删 0 行，
        mutation 重新提交（旧 mutation 完成与否不阻塞 —— 以新 id 为准），
        S3 前缀再删一遍。幂等是"崩溃后从断点续跑"的前提。
        """
        from ariadne.storage.deleters import CLICKHOUSE_TABLES

        assert self._pg_deleter is not None
        assert self._ch_deleter is not None
        assert self._os_deleter is not None

        try:
            pg_deleted = await self._pg_deleter.delete_project_data(
                project_id, subject_id
            )
            await self._update(
                job_id,
                project_id,
                status="postgres_done",
                postgres_deleted=pg_deleted,
            )

            # spans 的 mutation_id 用来判完成；rollup 的不记录（subject 级删除
            # 时 rollup 会被跳过，返回空 id）
            mutation_id = ""
            for table in CLICKHOUSE_TABLES:
                mid = await asyncio.to_thread(
                    self._ch_deleter.submit_delete_mutation,
                    table,
                    project_id,
                    subject_id,
                )
                if table == "spans":
                    mutation_id = mid
            await self._update(
                job_id,
                project_id,
                status=_MUTATION_SUBMITTED,
                clickhouse_mutation_id=mutation_id,
            )

            s3_deleted = await asyncio.to_thread(
                self._os_deleter.delete_prefix, f"{project_id}/"
            )
            await self._update(
                job_id, project_id, status=_S3_DONE, s3_deleted=s3_deleted
            )

            logger.info(
                "GDPR 级联删除已执行，等待 ClickHouse mutation 完成",
                extra={
                    "job_id": str(job_id),
                    "postgres_deleted": pg_deleted,
                    "s3_deleted": s3_deleted,
                    "mutation_id": mutation_id,
                },
            )
        except Exception as exc:
            logger.error(
                "GDPR 级联删除失败",
                extra={"job_id": str(job_id), "error": str(exc)},
                exc_info=True,
            )
            await self._update(
                job_id, project_id, status=_FAILED, error=str(exc)[:2000]
            )
            self._stats["failed"] += 1
            gdpr_deletion_total.labels(status="failed").inc()

    async def _check_mutation(self, job_id: UUID, project_id: UUID) -> None:
        """轮询 ClickHouse mutation，完成则标 completed。"""
        assert self._ch_deleter is not None

        assert self._pg is not None
        async with tenant_session(self._pg, project_id) as session:
            row = await session.execute(
                select(
                    DeletionJobRow.clickhouse_mutation_id, DeletionJobRow.status
                ).where(DeletionJobRow.id == job_id)
            )
            found = row.first()
        if found is None:
            return
        mutation_id, status = found[0], found[1]

        # s3_done 之前不能报 completed：mutation 完成不代表 S3 删完了
        if status != _S3_DONE:
            self._stats["awaiting"] += 1
            return

        done = await asyncio.to_thread(self._ch_deleter.is_mutation_done, mutation_id)
        if not done:
            self._stats["awaiting"] += 1
            return

        await self._update(
            job_id,
            project_id,
            status=_COMPLETED,
            claimed_by=None,
            lease_expires_at=None,
        )
        self._stats["completed"] += 1
        gdpr_deletion_total.labels(status="completed").inc()
        logger.info("GDPR 删除任务完成", extra={"job_id": str(job_id)})

    async def _update(self, job_id: UUID, project_id: UUID, **fields: Any) -> None:
        """状态落库。必须走租户会话：RLS 对 UPDATE 同样生效，裸更新在
        生产（app 角色）不是越权就是静默零行。"""
        assert self._pg is not None
        async with tenant_session(self._pg, project_id) as session:
            await session.execute(
                update(DeletionJobRow)
                .where(DeletionJobRow.id == job_id)
                .values(**fields)
            )

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    async def close(self) -> None:
        if self._pg is not None:
            await self._pg.close()


async def run_retention_worker() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    worker = RetentionWorker(settings)

    loop = asyncio.get_running_loop()
    task = asyncio.create_task(worker.start())

    def _shutdown() -> None:
        logger.info("shutdown signal received")
        asyncio.create_task(worker.stop()).add_done_callback(lambda _: None)

    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows 不支持
            loop.add_signal_handler(sig, _shutdown)

    try:
        await task
    finally:
        await worker.close()


__all__ = ["RetentionWorker", "run_retention_worker"]
