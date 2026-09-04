"""TTL / 冷热分层 / GDPR 级联删除。

M6 Week 3 交付。分两个职责：

1. **保留策略**：ClickHouse TTL 由 DDL 自动执行（003_retention.sql），
   此模块提供保留配置 + 状态查询接口。

2. **GDPR 级联删除**：跨三处存储（Postgres / ClickHouse / S3）删除指定
   project 或 subject 的数据。ClickHouse 的 ALTER TABLE DELETE 是异步
   mutation，因此需要记录删除任务状态（M6 §4.3）。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from ariadne.observability.metrics import gdpr_deletion_total

logger = logging.getLogger(__name__)


class DeletionStatus(StrEnum):
    """GDPR 删除任务状态。"""

    PENDING = "pending"
    POSTGRES_DONE = "postgres_done"
    CLICKHOUSE_MUTATION_SUBMITTED = "clickhouse_mutation_submitted"
    S3_DONE = "s3_done"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class DeletionJob:
    """GDPR 删除任务记录。"""

    id: UUID
    project_id: UUID
    subject_id: str  # 可选的 subject 级删除（如 user_id）
    status: DeletionStatus
    started_at: datetime
    updated_at: datetime
    error: str = ""
    # ClickHouse mutation ID（用于进度查询）
    clickhouse_mutation_id: str = ""
    postgres_deleted: int = 0
    clickhouse_deleted: int = 0
    s3_deleted: int = 0


class PostgresDeleter(Protocol):
    """Postgres 删除接口。"""

    async def delete_project_data(
        self, project_id: UUID, subject_id: str = ""
    ) -> int:
        """删除项目数据，返回删除行数。"""
        ...


class ClickHouseDeleter(Protocol):
    """ClickHouse 删除接口。"""

    def submit_delete_mutation(
        self, table: str, project_id: UUID, subject_id: str = ""
    ) -> str:
        """提交 ALTER TABLE DELETE mutation，返回 mutation ID。"""
        ...

    def is_mutation_done(self, mutation_id: str) -> bool:
        """查询 mutation 是否完成。"""
        ...


class ObjectStoreDeleter(Protocol):
    """对象存储删除接口。"""

    def delete_prefix(self, prefix: str) -> int:
        """删除指定前缀下所有对象，返回删除数量。"""
        ...


@dataclass
class RetentionManager:
    """保留策略 + GDPR 级联删除协调器。

    保留策略（TTL）由 ClickHouse DDL 自动执行，此模块不主动触发。
    GDPR 删除是主动操作，跨三处存储级联。
    """

    _postgres: PostgresDeleter
    _clickhouse: ClickHouseDeleter
    _object_store: ObjectStoreDeleter
    _jobs: dict[UUID, DeletionJob] = field(default_factory=dict)

    async def request_deletion(
        self,
        project_id: UUID,
        subject_id: str = "",
    ) -> DeletionJob:
        """发起 GDPR 删除请求。

        返回 DeletionJob，调用方可轮询 get_job_status 查询进度。
        不承诺"立即删除"——ClickHouse mutation 是异步的（M6 §4.3）。
        """
        job_id = uuid.uuid4()
        now = datetime.now(UTC)
        job = DeletionJob(
            id=job_id,
            project_id=project_id,
            subject_id=subject_id,
            status=DeletionStatus.PENDING,
            started_at=now,
            updated_at=now,
        )
        self._jobs[job_id] = job
        await self._execute_cascade(job)
        return self._jobs[job_id]

    async def _execute_cascade(self, job: DeletionJob) -> None:
        """执行三处存储的级联删除。"""
        try:
            # 1. Postgres：FK 级联删除（同步）
            pg_deleted = await self._postgres.delete_project_data(
                job.project_id, job.subject_id
            )
            self._update_job(
                job.id,
                status=DeletionStatus.POSTGRES_DONE,
                postgres_deleted=pg_deleted,
            )

            # 2. ClickHouse：提交异步 mutation（spans + rollups）
            ch_mutation = self._clickhouse.submit_delete_mutation(
                "spans", job.project_id, job.subject_id
            )
            self._clickhouse.submit_delete_mutation(
                "trace_rollup", job.project_id, job.subject_id
            )
            self._clickhouse.submit_delete_mutation(
                "cost_rollup", job.project_id, job.subject_id
            )
            self._update_job(
                job.id,
                status=DeletionStatus.CLICKHOUSE_MUTATION_SUBMITTED,
                clickhouse_mutation_id=ch_mutation,
            )

            # 3. S3 / 对象存储：按 project_id 前缀删除
            s3_prefix = f"{job.project_id}/"
            s3_deleted = self._object_store.delete_prefix(s3_prefix)
            self._update_job(
                job.id,
                status=DeletionStatus.S3_DONE,
                s3_deleted=s3_deleted,
            )

            # 4. 检查 ClickHouse mutation 是否完成（轮询由调用方做）
            # 此处只标记 S3 完成，最终 COMPLETED 由 check_mutation 设置
            if self._clickhouse.is_mutation_done(ch_mutation):
                self._update_job(
                    job.id, status=DeletionStatus.COMPLETED
                )
                gdpr_deletion_total.labels(status="completed").inc()

        except Exception as exc:
            logger.error(
                "GDPR deletion cascade failed",
                extra={"job_id": str(job.id), "error": str(exc)},
            )
            self._update_job(
                job.id, status=DeletionStatus.FAILED, error=str(exc)
            )
            gdpr_deletion_total.labels(status="failed").inc()

    def check_and_complete(self, job_id: UUID) -> DeletionJob | None:
        """检查 ClickHouse mutation 是否完成，完成则标记 COMPLETED。"""
        job = self._jobs.get(job_id)
        if job is None or job.status not in (
            DeletionStatus.S3_DONE,
            DeletionStatus.CLICKHOUSE_MUTATION_SUBMITTED,
        ):
            return job
        if self._clickhouse.is_mutation_done(job.clickhouse_mutation_id):
            self._update_job(job_id, status=DeletionStatus.COMPLETED)
            gdpr_deletion_total.labels(status="completed").inc()
        return self._jobs.get(job_id)

    def get_job(self, job_id: UUID) -> DeletionJob | None:
        return self._jobs.get(job_id)

    def _update_job(self, job_id: UUID, **fields: Any) -> None:
        """更新任务字段（immutable replace）。"""
        job = self._jobs[job_id]
        updated = replace(job, **fields, updated_at=datetime.now(UTC))
        self._jobs[job_id] = updated


__all__ = [
    "ClickHouseDeleter",
    "DeletionJob",
    "DeletionStatus",
    "ObjectStoreDeleter",
    "PostgresDeleter",
    "RetentionManager",
]
