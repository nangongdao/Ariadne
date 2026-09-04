"""Retention Worker 与真实 deleter 的测试。

刻意用真 sqlite 引擎跑 Postgres 侧删除：`test_retention.py` 里三个 Deleter
全是 Fake，于是"删除逻辑"从未碰过真 SQL —— 我在写 PostgresCascadeDeleter
时正是靠这条才发现 loop_runs 根本没有 metadata_json 列（记忆里 FakeStore
掩盖 SQL 缺陷是同一个模式）。Fake 只用在 ClickHouse / S3 侧：mutation 与
对象存储没有本地等价物。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ariadne.storage.deleters import (
    CLICKHOUSE_TABLES,
    ClickHouseMutationDeleter,
    PostgresCascadeDeleter,
)
from ariadne.storage.postgres.retention_models import DeletionJobRow
from ariadne.worker.retention_worker import RetentionWorker

ORG_ID = UUID("00000000-0000-0000-0000-0000000000aa")
PROJECT_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_PROJECT = UUID("00000000-0000-0000-0000-000000000002")


class FakeClickHouseDeleter:
    """记录提交的 mutation。mutations_done 控制轮询结果。"""

    def __init__(self, mutations_done: bool = True) -> None:
        self.submitted: list[tuple[str, UUID, str]] = []
        self.mutations_done = mutations_done

    def submit_delete_mutation(
        self, table: str, project_id: UUID, subject_id: str = ""
    ) -> str:
        self.submitted.append((table, project_id, subject_id))
        return f"mut-{table}"

    def is_mutation_done(self, mutation_id: str) -> bool:
        return self.mutations_done


class FakeObjectStoreDeleter:
    def __init__(self, count: int = 3) -> None:
        self.count = count
        self.prefixes: list[str] = []

    def delete_prefix(self, prefix: str) -> int:
        self.prefixes.append(prefix)
        return self.count


async def make_pg() -> Any:
    """建 sqlite 内存库并注册全部表 —— 删除要跨表，metadata 必须齐。"""
    from ariadne.storage.postgres import (  # noqa: F401 — 注册表到 metadata
        auth_models,
        graph_models,
        harness_models,
        loop_models,
        model_config_models,
        retention_models,
    )
    from ariadne.storage.postgres.models import Base, Organization, Project

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as s:
        s.add(Organization(id=ORG_ID, name="org"))
        s.add(Project(id=PROJECT_ID, org_id=ORG_ID, slug="p1", name="P1"))
        s.add(Project(id=OTHER_PROJECT, org_id=ORG_ID, slug="p2", name="P2"))
        await s.commit()

    class MemoryPg:
        @asynccontextmanager
        async def session(self) -> AsyncIterator[Any]:
            async with maker() as s:
                try:
                    yield s
                    await s.commit()
                except Exception:
                    await s.rollback()
                    raise

        async def close(self) -> None:
            await engine.dispose()

    return MemoryPg()


async def seed_loop_run(pg: Any, project_id: UUID, subject: str = "") -> UUID:
    from ariadne.storage.postgres.loop_models import LoopRun

    run_id = uuid.uuid4()
    goal: dict[str, Any] = {"task": "t", "assertions": []}
    if subject:
        goal["subject_id"] = subject
    async with pg.session() as s:
        s.add(
            LoopRun(
                id=run_id,
                project_id=project_id,
                mode="single",
                goal=goal,
                state="EXECUTING",
            )
        )
    return run_id


async def seed_job(pg: Any, project_id: UUID, subject: str = "") -> UUID:
    job_id = uuid.uuid4()
    async with pg.session() as s:
        s.add(
            DeletionJobRow(
                id=job_id,
                project_id=project_id,
                subject_id=subject,
                status="pending",
            )
        )
    return job_id


async def job_status(pg: Any, job_id: UUID) -> str:
    async with pg.session() as s:
        row = await s.execute(
            select(DeletionJobRow.status).where(DeletionJobRow.id == job_id)
        )
        return str(row.scalar_one())


class TestPostgresCascadeDeleter:
    """真 SQL 跑一遍 —— Fake 版本发现不了列名错误。"""

    async def test_deletes_project_rows(self) -> None:
        pg = await make_pg()
        await seed_loop_run(pg, PROJECT_ID)
        await seed_loop_run(pg, PROJECT_ID)

        deleted = await PostgresCascadeDeleter(pg).delete_project_data(PROJECT_ID)
        assert deleted == 2

    async def test_does_not_touch_other_projects(self) -> None:
        """跨租户误删是这类批量删除最危险的失效模式。"""
        pg = await make_pg()
        await seed_loop_run(pg, PROJECT_ID)
        keep = await seed_loop_run(pg, OTHER_PROJECT)

        await PostgresCascadeDeleter(pg).delete_project_data(PROJECT_ID)

        from ariadne.storage.postgres.loop_models import LoopRun

        async with pg.session() as s:
            rows = (await s.execute(select(LoopRun.id))).scalars().all()
        assert list(rows) == [keep]

    async def test_subject_delete_only_matching_rows(self) -> None:
        pg = await make_pg()
        await seed_loop_run(pg, PROJECT_ID, subject="alice")
        keep = await seed_loop_run(pg, PROJECT_ID, subject="bob")

        deleted = await PostgresCascadeDeleter(pg).delete_project_data(
            PROJECT_ID, "alice"
        )
        assert deleted == 1

        from ariadne.storage.postgres.loop_models import LoopRun

        async with pg.session() as s:
            rows = (await s.execute(select(LoopRun.id))).scalars().all()
        assert list(rows) == [keep]

    async def test_subject_delete_treats_like_metacharacters_literally(self) -> None:
        """subject_id 是 JSON 值，不应被 SQL LIKE 通配符解释。"""
        pg = await make_pg()
        keep = await seed_loop_run(pg, PROJECT_ID, subject="alice")
        await seed_loop_run(pg, PROJECT_ID, subject="bob")

        deleted = await PostgresCascadeDeleter(pg).delete_project_data(
            PROJECT_ID, "%"
        )
        assert deleted == 0

        from ariadne.storage.postgres.loop_models import LoopRun

        async with pg.session() as s:
            rows = (await s.execute(select(LoopRun.id))).scalars().all()
        assert len(rows) == 2
        assert keep in rows


class TestClickHouseMutationDeleter:
    """只测不需要真库的部分：表名白名单与 rollup 跳过逻辑。"""

    def test_rejects_unknown_table(self) -> None:
        """表名会拼进 SQL，必须白名单校验。"""
        deleter = ClickHouseMutationDeleter.__new__(ClickHouseMutationDeleter)
        deleter._database = "ariadne"  # type: ignore[attr-defined]
        with pytest.raises(ValueError, match="不允许删除的表"):
            deleter.submit_delete_mutation("spans; DROP TABLE x", PROJECT_ID)

    def test_subject_delete_skips_rollup_tables(self) -> None:
        """rollup 无 subject 维度，按 subject 删会误删整个项目的聚合。"""
        deleter = ClickHouseMutationDeleter.__new__(ClickHouseMutationDeleter)
        deleter._database = "ariadne"  # type: ignore[attr-defined]
        assert deleter.submit_delete_mutation("trace_rollup", PROJECT_ID, "alice") == ""

    def test_empty_mutation_id_counts_as_done(self) -> None:
        deleter = ClickHouseMutationDeleter.__new__(ClickHouseMutationDeleter)
        assert deleter.is_mutation_done("") is True


class TestRetentionWorkerCascade:
    """装配断言：worker 真的消费 pending 行并推进状态机。"""

    async def make_worker(
        self, pg: Any, *, mutations_done: bool = True
    ) -> tuple[RetentionWorker, FakeClickHouseDeleter, FakeObjectStoreDeleter]:
        ch = FakeClickHouseDeleter(mutations_done=mutations_done)
        s3 = FakeObjectStoreDeleter()
        worker = RetentionWorker(
            pg=pg,
            postgres_deleter=PostgresCascadeDeleter(pg),
            clickhouse_deleter=ch,
            object_store_deleter=s3,
        )
        return worker, ch, s3

    async def test_pending_job_reaches_completed(self) -> None:
        pg = await make_pg()
        await seed_loop_run(pg, PROJECT_ID)
        job_id = await seed_job(pg, PROJECT_ID)

        worker, ch, s3 = await self.make_worker(pg)
        # mutation 立即完成时一轮就走完：run_once 执行级联后会在同一轮
        # 复查 awaiting 状态的任务，不必等下一次轮询
        await worker.run_once()
        assert await job_status(pg, job_id) == "completed"
        assert worker.stats["completed"] == 1

        assert [t for t, _, _ in ch.submitted] == list(CLICKHOUSE_TABLES)
        assert s3.prefixes == [f"{PROJECT_ID}/"]

    async def test_pending_mutation_stays_awaiting(self) -> None:
        """mutation 未完成时不得报 completed —— 那会谎报删除已完成。"""
        pg = await make_pg()
        job_id = await seed_job(pg, PROJECT_ID)

        worker, _ch, _s3 = await self.make_worker(pg, mutations_done=False)
        await worker.run_once()
        await worker.run_once()
        assert await job_status(pg, job_id) == "s3_done"
        assert worker.stats["completed"] == 0

    async def test_records_deleted_counts(self) -> None:
        pg = await make_pg()
        await seed_loop_run(pg, PROJECT_ID)
        job_id = await seed_job(pg, PROJECT_ID)

        worker, _ch, _s3 = await self.make_worker(pg)
        await worker.run_once()

        async with pg.session() as s:
            row = await s.execute(
                select(
                    DeletionJobRow.postgres_deleted, DeletionJobRow.s3_deleted
                ).where(DeletionJobRow.id == job_id)
            )
            pg_deleted, s3_deleted = row.one()
        assert pg_deleted == 1
        assert s3_deleted == 3

    async def test_failure_marks_job_failed(self) -> None:
        """失败必须落库 —— 静默失败的删除任务是最坏的合规结果。"""
        pg = await make_pg()
        job_id = await seed_job(pg, PROJECT_ID)

        class FailingPg:
            async def delete_project_data(
                self, project_id: UUID, subject_id: str = ""
            ) -> int:
                raise RuntimeError("pg down")

        worker = RetentionWorker(
            pg=pg,
            postgres_deleter=FailingPg(),
            clickhouse_deleter=FakeClickHouseDeleter(),
            object_store_deleter=FakeObjectStoreDeleter(),
        )
        await worker.run_once()

        assert await job_status(pg, job_id) == "failed"
        async with pg.session() as s:
            err = await s.execute(
                select(DeletionJobRow.error).where(DeletionJobRow.id == job_id)
            )
            assert "pg down" in str(err.scalar_one())

    async def test_no_pending_jobs_is_noop(self) -> None:
        pg = await make_pg()
        worker, _ch, _s3 = await self.make_worker(pg)
        assert await worker.run_once() == 0

    async def test_subject_job_passes_subject_to_deleters(self) -> None:
        pg = await make_pg()
        await seed_job(pg, PROJECT_ID, subject="alice")

        worker, ch, _s3 = await self.make_worker(pg)
        await worker.run_once()

        assert all(sub == "alice" for _t, _p, sub in ch.submitted)


class TestClaimLease:
    """P1-8：认领租约语义 —— 多副本防双删 + 崩溃续跑。"""

    async def test_run_once_builds_deleters_lazily(self) -> None:
        """run_once 单独调用（运维排障/验收入口）必须可用。

        此前 _build_deleters 只在 start() 里执行，直接调 run_once 会在
        assert self._pg 上炸 —— 真机验收（容器内单轮处理）抓到的。

        ClickHouse 不可用时 skip（懒装配走真实连接，无容器必失败）。
        """
        from ariadne.config import ClickHouseSettings
        from ariadne.storage.clickhouse import ClickHouseStore

        ch = ClickHouseStore(ClickHouseSettings())
        if not ch.ping():
            pytest.skip("ClickHouse 不可用，先 docker compose up -d clickhouse")

        pg = await make_pg()
        job_id = await seed_job(pg, PROJECT_ID)
        worker = RetentionWorker(pg=pg)  # 不注入任何 deleter
        assert await worker.run_once() >= 1
        assert await job_status(pg, job_id) == "completed"

    async def make_worker(
        self, pg: Any, *, mutations_done: bool = True
    ) -> tuple[RetentionWorker, FakeClickHouseDeleter, FakeObjectStoreDeleter]:
        ch = FakeClickHouseDeleter(mutations_done=mutations_done)
        s3 = FakeObjectStoreDeleter()
        worker = RetentionWorker(
            pg=pg,
            postgres_deleter=PostgresCascadeDeleter(pg),
            clickhouse_deleter=ch,
            object_store_deleter=s3,
        )
        return worker, ch, s3

    async def test_claim_writes_lease(self) -> None:
        pg = await make_pg()
        job_id = await seed_job(pg, PROJECT_ID)
        worker, _ch, _s3 = await self.make_worker(pg)

        claimed = await worker._claim_cascade_jobs()
        assert [(r[0], r[1]) for r in claimed] == [(job_id, PROJECT_ID)]

        async with pg.session() as s:
            row = await s.execute(
                select(
                    DeletionJobRow.claimed_by, DeletionJobRow.lease_expires_at
                ).where(DeletionJobRow.id == job_id)
            )
            claimed_by, lease = row.one()
        assert claimed_by == worker._worker_id
        assert lease is not None

    async def test_active_lease_blocks_second_claim(self) -> None:
        """副本 A 持有租约期间，副本 B 认领不到同一任务。"""
        pg = await make_pg()
        await seed_job(pg, PROJECT_ID)
        worker_a, _ch_a, _s3_a = await self.make_worker(pg)
        worker_b, _ch_b, _s3_b = await self.make_worker(pg)

        assert len(await worker_a._claim_cascade_jobs()) == 1
        assert await worker_b._claim_cascade_jobs() == []

    async def test_expired_lease_reclaimed(self) -> None:
        """持有者崩溃后租约过期，其他副本可重新认领并执行。"""
        pg = await make_pg()
        job_id = await seed_job(pg, PROJECT_ID)
        worker_a, _ch_a, _s3_a = await self.make_worker(pg)
        worker_b, ch_b, _s3_b = await self.make_worker(pg)

        assert len(await worker_a._claim_cascade_jobs()) == 1
        # 模拟 A 崩溃：租约过期
        from datetime import UTC, datetime, timedelta

        async with pg.session() as s:
            await s.execute(
                DeletionJobRow.__table__.update()
                .where(DeletionJobRow.id == job_id)
                .values(
                    lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                    status="pending",
                )
            )

        await worker_b.run_once()
        assert ch_b.submitted  # B 真的执行了级联
        assert await job_status(pg, job_id) == "completed"

    async def test_stuck_postgres_done_job_resumes(self) -> None:
        """级联中途崩溃停在 postgres_done 的任务从断点续跑到完成。

        此前 postgres_done 不在任何获取路径里，崩在这一步的任务永远卡住。
        """
        pg = await make_pg()
        job_id = await seed_job(pg, PROJECT_ID)
        worker, ch, s3 = await self.make_worker(pg)

        # 上一世 Worker 崩在级联中途，留下 postgres_done + 过期租约
        from datetime import UTC, datetime, timedelta

        async with pg.session() as s:
            await s.execute(
                DeletionJobRow.__table__.update()
                .where(DeletionJobRow.id == job_id)
                .values(
                    status="postgres_done",
                    postgres_deleted=1,
                    lease_expires_at=datetime.now(UTC) - timedelta(seconds=1),
                )
            )

        await worker.run_once()
        assert await job_status(pg, job_id) == "completed"
        assert ch.submitted
        assert s3.prefixes == [f"{PROJECT_ID}/"]

    async def test_completed_clears_lease(self) -> None:
        pg = await make_pg()
        await seed_loop_run(pg, PROJECT_ID)
        job_id = await seed_job(pg, PROJECT_ID)
        worker, _ch, _s3 = await self.make_worker(pg)

        await worker.run_once()
        assert await job_status(pg, job_id) == "completed"
        async with pg.session() as s:
            row = await s.execute(
                select(
                    DeletionJobRow.claimed_by, DeletionJobRow.lease_expires_at
                ).where(DeletionJobRow.id == job_id)
            )
            claimed_by, lease = row.one()
        assert claimed_by is None
        assert lease is None


class TestCliEntry:
    """R12 防御：worker 有生产入口，否则又是一个"建好但没人调"。"""

    async def test_retention_subcommand_registered(self) -> None:
        import inspect

        from ariadne import cli

        source = inspect.getsource(cli.run_worker)
        assert '"retention"' in source
        assert "run_retention_worker" in source
