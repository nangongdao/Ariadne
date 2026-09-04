"""实验仓储。

状态机：pending → running → completed / failed / cancelled。
转移用显式方法而非直接改字段 —— 状态流转对一致性敏感，
散落的 `exp.status = "x"` 会让"什么时候能转到什么状态"无从追溯。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from ariadne.experiment.persist import to_dict
from ariadne.experiment.runner import ExperimentResult
from ariadne.storage.postgres.models import Experiment as ExperimentRow

ExperimentStatus = Literal["pending", "running", "completed", "failed", "cancelled"]

# 合法转移。终态不再转移 —— 与 M3 的 Loop 状态机同一原则
_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "cancelled"}),
    "running": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


class ExperimentNotFoundError(LookupError):
    pass


class InvalidTransitionError(ValueError):
    """非法状态转移。"""


class ExperimentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        project_id: uuid.UUID,
        dataset_ref: str,
        config_label: str,
        config: dict[str, Any],
        dataset_id: uuid.UUID | None = None,
        judge_models: tuple[str, ...] = (),
        item_count: int = 0,
    ) -> uuid.UUID:
        row = ExperimentRow(
            id=uuid.uuid4(),
            project_id=project_id,
            dataset_id=dataset_id,
            dataset_ref=dataset_ref,
            config_label=config_label,
            config=config,
            # 存成 {"models": [...]}: JSON 列存裸数组在部分驱动上行为不一致
            judge_models={"models": list(judge_models)},
            item_count=item_count,
            status="pending",
        )
        self._session.add(row)
        await self._session.flush()
        return row.id

    async def _load(
        self, *, project_id: uuid.UUID, experiment_id: uuid.UUID
    ) -> ExperimentRow:
        row = (
            await self._session.execute(
                select(ExperimentRow).where(
                    ExperimentRow.id == experiment_id,
                    ExperimentRow.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise ExperimentNotFoundError(f"实验不存在: {experiment_id}")
        return row

    async def get_with_snapshot(
        self, *, project_id: uuid.UUID, experiment_id: uuid.UUID
    ) -> ExperimentRow:
        """取实验行并一并加载 deferred 的 result_snapshot。

        对比用。异步会话里访问 deferred 列会触发隐式 IO 报错，
        必须在查询时 undefer 而不能事后惰性加载。
        """
        row = (
            await self._session.execute(
                select(ExperimentRow)
                .where(
                    ExperimentRow.id == experiment_id,
                    ExperimentRow.project_id == project_id,
                )
                .options(undefer(ExperimentRow.result_snapshot))
            )
        ).scalar_one_or_none()
        if row is None:
            raise ExperimentNotFoundError(f"实验不存在: {experiment_id}")
        return row

    async def transition(
        self,
        *,
        project_id: uuid.UUID,
        experiment_id: uuid.UUID,
        to: ExperimentStatus,
        error: str = "",
    ) -> None:
        row = await self._load(project_id=project_id, experiment_id=experiment_id)
        allowed = _TRANSITIONS.get(row.status, frozenset())
        if to not in allowed:
            raise InvalidTransitionError(
                f"实验 {experiment_id} 不能从 {row.status} 转到 {to}"
                f"（允许: {sorted(allowed) or '无，已是终态'}）"
            )

        row.status = to
        if error:
            row.error = error
        if to in {"completed", "failed", "cancelled"}:
            row.finished_at = datetime.now(UTC)
        await self._session.flush()

    async def save_result(
        self,
        *,
        project_id: uuid.UUID,
        experiment_id: uuid.UUID,
        result: ExperimentResult,
    ) -> None:
        """写入聚合结果并转 completed，同时留下逐样本快照。

        聚合指标进独立列供列表查询；逐样本进 deferred 的 result_snapshot，
        因为 compare 的置信区间 / churn / flipped_to_fail 只有逐样本能算。
        正式方案是 ClickHouse 的 eval_results 表（见 docs/08 分工判据），
        待容器环境可用后迁移。
        """
        row = await self._load(project_id=project_id, experiment_id=experiment_id)
        if row.status not in {"pending", "running"}:
            raise InvalidTransitionError(
                f"实验 {experiment_id} 已是 {row.status}，不能再写入结果"
            )

        row.item_count = len(result.outcomes)
        row.failed_count = len(result.generation_failures)
        row.total_cost_usd = result.total_cost
        row.metrics = {
            **result.metrics(),
            "evaluators": result.evaluator_metrics(),
            "failure_signatures": result.failure_signature_counts(),
        }
        row.judge_models = {
            "models": sorted(
                {
                    r.judge_model
                    for o in result.outcomes
                    for r in o.results
                    if r.judge_model
                }
            )
        }
        row.result_snapshot = to_dict(result)
        row.status = "completed"
        row.finished_at = datetime.now(UTC)
        await self._session.flush()

    async def get(
        self, *, project_id: uuid.UUID, experiment_id: uuid.UUID
    ) -> ExperimentRow:
        return await self._load(project_id=project_id, experiment_id=experiment_id)

    async def list_recent(
        self,
        *,
        project_id: uuid.UUID,
        limit: int = 50,
        status: ExperimentStatus | None = None,
        dataset_ref: str | None = None,
    ) -> list[ExperimentRow]:
        stmt = select(ExperimentRow).where(ExperimentRow.project_id == project_id)
        if status is not None:
            stmt = stmt.where(ExperimentRow.status == status)
        if dataset_ref is not None:
            stmt = stmt.where(ExperimentRow.dataset_ref == dataset_ref)

        stmt = stmt.order_by(ExperimentRow.created_at.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    async def find_baseline(
        self, *, project_id: uuid.UUID, dataset_ref: str, config_label: str
    ) -> ExperimentRow | None:
        """找同数据集同配置的最近一次成功实验，作为对比基线。

        限定 dataset_ref 相同：在不同数据集上比均值毫无意义
        （compare() 会拒绝，这里提前过滤避免拿到不可比的基线）。
        """
        return (
            await self._session.execute(
                select(ExperimentRow)
                .where(
                    ExperimentRow.project_id == project_id,
                    ExperimentRow.dataset_ref == dataset_ref,
                    ExperimentRow.config_label == config_label,
                    ExperimentRow.status == "completed",
                )
                .order_by(ExperimentRow.finished_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def cost_summary(
        self, *, project_id: uuid.UUID, days: int = 30
    ) -> dict[str, Decimal | int]:
        """实验成本汇总。评测本身也花钱，必须可见。"""
        from datetime import timedelta

        since = datetime.now(UTC) - timedelta(days=days)
        result = await self._session.execute(
            select(
                func.count(ExperimentRow.id),
                func.coalesce(func.sum(ExperimentRow.total_cost_usd), 0),
                func.coalesce(func.sum(ExperimentRow.item_count), 0),
            ).where(
                ExperimentRow.project_id == project_id,
                ExperimentRow.created_at >= since,
                ExperimentRow.status == "completed",
            )
        )
        count, cost, items = result.one()
        return {
            "experiment_count": int(count),
            "total_cost_usd": Decimal(str(cost)),
            "total_items": int(items),
        }


def judge_models_of(row: ExperimentRow) -> tuple[str, ...]:
    """读出 Judge 模型列表。"""
    raw = row.judge_models or {}
    models = raw.get("models", []) if isinstance(raw, dict) else []
    return tuple(str(m) for m in models)
