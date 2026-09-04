"""实验与对比 API。

M2 阶段实验由客户端（SDK / CLI）执行后回传结果，服务端只负责持久化与对比。
理由：实验要调用用户自己的模型与 provider 密钥，服务端代跑意味着要托管
密钥（见 docs/10 的密钥管理决策，默认不存）。M5 的编排能力才引入服务端执行。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field

from ariadne.api.deps import TenantCtx, TenantPg
from ariadne.api.errors import BadRequestError, ConflictError, NotFoundError
from ariadne.auth.rbac import Permission, check_permission
from ariadne.experiment.compare import DatasetMismatchError, compare, format_report
from ariadne.experiment.gate import DEFAULT_RULES, parse_rules
from ariadne.experiment.persist import from_dict, to_dict
from ariadne.storage.postgres.repositories import (
    ExperimentNotFoundError,
    ExperimentRepository,
    InvalidTransitionError,
    judge_models_of,
)

router = APIRouter(tags=["experiments"])


class CreateExperimentRequest(BaseModel):
    dataset_ref: str = Field(min_length=1, max_length=300)
    config_label: str = Field(min_length=1, max_length=200)
    config: dict[str, Any] = Field(default_factory=dict)
    dataset_id: UUID | None = None
    item_count: int = Field(default=0, ge=0)


class ExperimentSummary(BaseModel):
    id: UUID
    dataset_ref: str
    config_label: str
    status: str
    item_count: int
    failed_count: int
    total_cost_usd: Decimal
    metrics: dict[str, Any]
    judge_models: list[str]
    created_at: datetime
    finished_at: datetime | None
    error: str


class SubmitResultRequest(BaseModel):
    """回传实验结果。

    result 是 persist.to_dict() 的输出 —— 客户端跑完实验后原样上传，
    格式带 schema_version，不兼容时给明确提示。
    """

    result: dict[str, Any]


class CompareRequest(BaseModel):
    baseline_id: UUID
    current_id: UUID
    # 门禁规则；缺省用 DEFAULT_RULES
    fail_if: list[dict[str, Any]] | None = None
    extra_metrics: list[str] = Field(default_factory=list)
    allow_dataset_mismatch: bool = False


class StatPayload(BaseModel):
    metric: str
    baseline_mean: float
    current_mean: float
    delta: float
    delta_pct: float
    ci_low: float
    ci_high: float
    direction: str
    significant: bool
    sample_count: int


class CompareResponse(BaseModel):
    passed: bool
    exit_code: int
    dataset_ref: str
    stats: list[StatPayload]
    churn: dict[str, int]
    violations: list[str]
    warnings: list[str]
    flipped_to_fail: list[str]
    report_text: str


def _to_summary(row: Any) -> ExperimentSummary:
    return ExperimentSummary(
        id=row.id,
        dataset_ref=row.dataset_ref,
        config_label=row.config_label,
        status=row.status,
        item_count=row.item_count,
        failed_count=row.failed_count,
        total_cost_usd=row.total_cost_usd,
        metrics=dict(row.metrics or {}),
        judge_models=list(judge_models_of(row)),
        created_at=row.created_at,
        finished_at=row.finished_at,
        error=row.error,
    )


@router.post(
    "/experiments",
    status_code=status.HTTP_201_CREATED,
    response_model=ExperimentSummary,
    summary="创建实验记录",
)
async def create_experiment(
    body: CreateExperimentRequest, ctx: TenantCtx, pg: TenantPg
) -> ExperimentSummary:
    check_permission(ctx.role, Permission.WRITE)
    async with pg.session() as session:
        repo = ExperimentRepository(session)
        experiment_id = await repo.create(
            project_id=ctx.project_id,
            dataset_ref=body.dataset_ref,
            config_label=body.config_label,
            config=body.config,
            dataset_id=body.dataset_id,
            item_count=body.item_count,
        )
        row = await repo.get(project_id=ctx.project_id, experiment_id=experiment_id)
        return _to_summary(row)


@router.get(
    "/experiments", response_model=list[ExperimentSummary], summary="实验列表"
)
async def list_experiments(
    ctx: TenantCtx,
    pg: TenantPg,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    dataset_ref: Annotated[str | None, Query()] = None,
) -> list[ExperimentSummary]:
    check_permission(ctx.role, Permission.READ)
    async with pg.session() as session:
        rows = await ExperimentRepository(session).list_recent(
            project_id=ctx.project_id, limit=limit, dataset_ref=dataset_ref
        )
    return [_to_summary(r) for r in rows]


@router.get(
    "/experiments/{experiment_id}",
    response_model=ExperimentSummary,
    summary="实验详情",
)
async def get_experiment(
    experiment_id: UUID, ctx: TenantCtx, pg: TenantPg
) -> ExperimentSummary:
    check_permission(ctx.role, Permission.READ)
    async with pg.session() as session:
        try:
            row = await ExperimentRepository(session).get(
                project_id=ctx.project_id, experiment_id=experiment_id
            )
        except ExperimentNotFoundError as exc:
            raise NotFoundError(str(exc), experiment_id=str(experiment_id)) from exc
    return _to_summary(row)


@router.post(
    "/experiments/{experiment_id}/result",
    response_model=ExperimentSummary,
    summary="回传实验结果",
)
async def submit_result(
    experiment_id: UUID,
    body: SubmitResultRequest,
    ctx: TenantCtx,
    pg: TenantPg,
) -> ExperimentSummary:
    check_permission(ctx.role, Permission.WRITE)
    try:
        result = from_dict(body.result)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc

    async with pg.session() as session:
        repo = ExperimentRepository(session)
        try:
            await repo.save_result(
                project_id=ctx.project_id, experiment_id=experiment_id, result=result
            )
        except ExperimentNotFoundError as exc:
            raise NotFoundError(str(exc)) from exc
        except InvalidTransitionError as exc:
            raise ConflictError(str(exc)) from exc

        row = await repo.get(project_id=ctx.project_id, experiment_id=experiment_id)
        return _to_summary(row)


@router.post(
    "/experiments/compare", response_model=CompareResponse, summary="对比两次实验"
)
async def compare_experiments(
    body: CompareRequest, ctx: TenantCtx, pg: TenantPg
) -> CompareResponse:
    """对比并执行门禁。

    注意：Postgres 只存聚合指标，逐样本明细在客户端上传的 result 里。
    因此对比需要两侧的完整 result —— 这里从 config 字段里取回存的快照。
    """
    # 对比不改任何状态，只读两侧结果算统计 —— 要 WRITE 会把 viewer/approver/
    # billing 全挡在外面，而看质量报告正是这些角色的日常
    check_permission(ctx.role, Permission.READ)
    async with pg.session() as session:
        repo = ExperimentRepository(session)
        try:
            baseline_row = await repo.get_with_snapshot(
                project_id=ctx.project_id, experiment_id=body.baseline_id
            )
            current_row = await repo.get_with_snapshot(
                project_id=ctx.project_id, experiment_id=body.current_id
            )
        except ExperimentNotFoundError as exc:
            raise NotFoundError(str(exc)) from exc

    baseline = _restore_result(baseline_row)
    current = _restore_result(current_row)

    rules = parse_rules(body.fail_if) if body.fail_if else DEFAULT_RULES
    try:
        report = compare(
            baseline,
            current,
            rules=rules,
            extra_metrics=tuple(body.extra_metrics),
            require_same_dataset=not body.allow_dataset_mismatch,
        )
    except DatasetMismatchError as exc:
        raise BadRequestError(str(exc)) from exc

    return CompareResponse(
        passed=report.passed,
        exit_code=report.exit_code,
        dataset_ref=report.dataset_ref,
        stats=[
            StatPayload(
                metric=s.metric,
                baseline_mean=s.baseline_mean,
                current_mean=s.current_mean,
                delta=s.delta,
                delta_pct=s.delta_pct,
                ci_low=s.ci_low,
                ci_high=s.ci_high,
                direction=s.direction.value,
                significant=s.significant,
                sample_count=s.sample_count,
            )
            for s in report.stats
        ],
        churn=report.churn,
        violations=[v.describe() for v in report.gate.violations],
        warnings=list(report.warnings),
        flipped_to_fail=sorted(report.flipped_to_fail),
        report_text=format_report(report),
    )


def _restore_result(row: Any) -> Any:
    """从实验行恢复完整结果。

    逐样本明细在 deferred 的 result_snapshot 列（由 save_result 写入）。
    正式方案是写 ClickHouse 的 eval_results 表（量大且只做聚合查询），
    待容器环境可用后迁移。
    """
    # 老数据的快照在 config 里（PUT /snapshot 的历史落点），一并兼容
    snapshot = row.result_snapshot or (row.config or {}).get("_result_snapshot")
    if not snapshot:
        raise BadRequestError(
            f"实验 {row.id} 没有逐样本快照，无法对比。"
            f"该实验可能是在快照落库前完成的；"
            f"请用 PUT /v1/experiments/{row.id}/snapshot 补传完整结果。"
        )
    return from_dict(snapshot)


class SnapshotRequest(BaseModel):
    result: dict[str, Any]


@router.put(
    "/experiments/{experiment_id}/snapshot",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="存入逐样本快照（M2 过渡方案）",
)
async def put_snapshot(
    experiment_id: UUID,
    body: SnapshotRequest,
    ctx: TenantCtx,
    pg: TenantPg,
) -> None:
    """存逐样本快照以支持对比。

    这是过渡方案，正式方案见 _restore_result 的说明。
    """
    check_permission(ctx.role, Permission.WRITE)
    try:
        result = from_dict(body.result)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc

    async with pg.session() as session:
        repo = ExperimentRepository(session)
        try:
            row = await repo.get(
                project_id=ctx.project_id, experiment_id=experiment_id
            )
        except ExperimentNotFoundError as exc:
            raise NotFoundError(str(exc)) from exc

        row.result_snapshot = to_dict(result)
