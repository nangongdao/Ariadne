"""保留策略 + GDPR 级联删除测试。

测试 RetentionManager 的级联删除协调逻辑（用 fake deleter）。
ClickHouse DDL 和 Alembic 迁移不在此测（需容器）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from ariadne.storage.retention import (
    DeletionJob,
    DeletionStatus,
    RetentionManager,
)

PROJECT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class FakePostgresDeleter:
    def __init__(self, rows: int = 42) -> None:
        self._rows = rows
        self.deleted: list[tuple[uuid.UUID, str]] = []

    async def delete_project_data(
        self, project_id: uuid.UUID, subject_id: str = ""
    ) -> int:
        self.deleted.append((project_id, subject_id))
        return self._rows


class FakeClickHouseDeleter:
    def __init__(self, mutations_done: bool = True) -> None:
        self._done = mutations_done
        self.mutations: list[tuple[str, uuid.UUID, str]] = []

    def submit_delete_mutation(
        self, table: str, project_id: uuid.UUID, subject_id: str = ""
    ) -> str:
        mut_id = f"mut-{len(self.mutations)}"
        self.mutations.append((table, project_id, subject_id))
        return mut_id

    def is_mutation_done(self, mutation_id: str) -> bool:
        return self._done


class FakeObjectStoreDeleter:
    def __init__(self, count: int = 10) -> None:
        self._count = count
        self.deleted_prefixes: list[str] = []

    def delete_prefix(self, prefix: str) -> int:
        self.deleted_prefixes.append(prefix)
        return self._count


class TestRetentionManager:
    async def test_cascade_deletion_full_flow(self) -> None:
        pg = FakePostgresDeleter(rows=42)
        ch = FakeClickHouseDeleter(mutations_done=True)
        s3 = FakeObjectStoreDeleter(count=10)

        manager = RetentionManager(pg, ch, s3)
        job = await manager.request_deletion(PROJECT_ID, subject_id="user-123")

        assert job.status == DeletionStatus.COMPLETED
        assert job.postgres_deleted == 42
        assert job.s3_deleted == 10
        assert len(ch.mutations) == 3  # spans + trace_rollup + cost_rollup
        assert s3.deleted_prefixes == [f"{PROJECT_ID}/"]
        assert pg.deleted == [(PROJECT_ID, "user-123")]

    async def test_clickhouse_mutation_pending(self) -> None:
        """mutation 未完成时状态停在 S3_DONE，不标记 COMPLETED。"""
        pg = FakePostgresDeleter()
        ch = FakeClickHouseDeleter(mutations_done=False)
        s3 = FakeObjectStoreDeleter()

        manager = RetentionManager(pg, ch, s3)
        job = await manager.request_deletion(PROJECT_ID)

        # mutation 未完成 → 停在 S3_DONE
        assert job.status == DeletionStatus.S3_DONE

        # 模拟 mutation 完成
        ch._done = True
        manager.check_and_complete(job.id)
        updated = manager.get_job(job.id)
        assert updated is not None
        assert updated.status == DeletionStatus.COMPLETED

    async def test_failed_deletion_records_error(self) -> None:
        class FailingPg:
            async def delete_project_data(
                self, project_id: uuid.UUID, subject_id: str = ""
            ) -> int:
                raise RuntimeError("connection refused")

        manager = RetentionManager(FailingPg(), FakeClickHouseDeleter(), FakeObjectStoreDeleter())
        job = await manager.request_deletion(PROJECT_ID)

        assert job.status == DeletionStatus.FAILED
        assert "connection refused" in job.error

    async def test_get_job_returns_none_for_unknown_id(self) -> None:
        manager = RetentionManager(
            FakePostgresDeleter(),
            FakeClickHouseDeleter(),
            FakeObjectStoreDeleter(),
        )
        assert manager.get_job(uuid.uuid4()) is None

    async def test_s3_prefix_uses_project_id(self) -> None:
        s3 = FakeObjectStoreDeleter(count=5)
        manager = RetentionManager(
            FakePostgresDeleter(),
            FakeClickHouseDeleter(),
            s3,
        )
        await manager.request_deletion(PROJECT_ID)

        # S3 前缀格式：{project_id}/ —— 第四层隔离
        assert s3.deleted_prefixes == [f"{PROJECT_ID}/"]

    async def test_clickhouse_submits_mutation_for_all_tables(self) -> None:
        ch = FakeClickHouseDeleter()
        manager = RetentionManager(
            FakePostgresDeleter(),
            ch,
            FakeObjectStoreDeleter(),
        )
        await manager.request_deletion(PROJECT_ID)

        tables = [m[0] for m in ch.mutations]
        assert "spans" in tables
        assert "trace_rollup" in tables
        assert "cost_rollup" in tables


class TestDeletionJob:
    def test_frozen_dataclass(self) -> None:
        """DeletionJob 是 frozen dataclass。"""
        job = DeletionJob(
            id=uuid.uuid4(),
            project_id=PROJECT_ID,
            subject_id="",
            status=DeletionStatus.PENDING,
            started_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        with pytest.raises(AttributeError):
            job.status = DeletionStatus.COMPLETED  # type: ignore[misc]

    def test_status_enum_values(self) -> None:
        assert DeletionStatus.PENDING.value == "pending"
        assert DeletionStatus.COMPLETED.value == "completed"
        assert DeletionStatus.FAILED.value == "failed"
