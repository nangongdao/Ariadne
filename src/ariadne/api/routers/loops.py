"""Loop REST API。

9 个端点（docs/09 1.1 节）：
- POST /v1/loops            创建（202 异步执行）
- GET  /v1/loops            列表
- GET  /v1/loops/{id}       详情
- GET  /v1/loops/{id}/iterations   轮次记录
- GET  /v1/loops/{id}/stream       SSE 实时事件
- POST /v1/loops/{id}/cancel      取消
- POST /v1/loops/{id}/resume      从检查点恢复
- POST /v1/loops/{id}/approve     HITL 审批
- POST /v1/loops/batch            批量创建

创建语义：API 只做"校验 + 落库 + 入队"，不执行。执行在 Worker。
这保证 API 无状态、可横向扩；也避免服务端持有 LLM key。

目标可验证性在 API 层提前拒绝（422），Worker 的 engine 会再验一次
（两层校验，edge 安全）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from ariadne.api.deps import AppSettings, TenantCtx, TenantPg
from ariadne.api.errors import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    UnprocessableError,
)
from ariadne.auth.rbac import Permission, check_permission
from ariadne.config import Settings
from ariadne.loop_module.artifact import (
    MAX_BYTES_PER_FILE,
    MAX_FILES_PER_WRITE,
    MAX_TOTAL_BYTES,
)
from ariadne.loop_module.goal import Assertion, AssertionKind, Budget, Goal, LoopMode
from ariadne.loop_module.goal_validation import validate_goal
from ariadne.loop_module.state_machine import LoopState
from ariadne.storage.postgres.loop_models import LoopCheckpointRow
from ariadne.storage.postgres.repositories.loop_runs import (
    LoopRunConflictError,
    LoopRunNotFoundError,
    LoopRunRepository,
    to_row_dict,
)
from ariadne.worker.loop_queue import LoopQueue

router = APIRouter(tags=["loops"])

# 单次批量创建上限。批量主要用于并行场景，太多会让单个请求过重。
MAX_BATCH_SIZE = 20


class AssertionPayload(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    kind: AssertionKind
    spec: dict[str, Any] = Field(default_factory=dict)
    weight: float = Field(default=1.0, ge=0.1)
    blocking: bool = True
    hint: str = ""


class BudgetPayload(BaseModel):
    max_iterations: int = Field(default=10, ge=1, le=50)
    max_total_tokens: int = Field(default=200_000, ge=1)
    max_cost_usd: float = Field(default=1.0, gt=0)
    max_tokens_per_iteration: int = Field(default=32_000, ge=1)
    max_wall_clock_seconds: int = Field(default=900, ge=1)


class CreateLoopRequest(BaseModel):
    task: str = Field(min_length=1, max_length=2000)
    mode: str = "quality"
    assertions: list[AssertionPayload] = Field(min_length=1, max_length=32)
    budget: BudgetPayload = Field(default_factory=BudgetPayload)
    stall_threshold: float = Field(default=2.0, ge=0)
    stall_patience: int = Field(default=2, ge=1)
    # 工作目录的种子文件：相对路径 → 内容。COMMAND 断言验证的对象从这里来。
    # 例如 {"solution.py": "...", "test_solution.py": "..."}。
    workspace: dict[str, str] = Field(
        default_factory=dict, max_length=MAX_FILES_PER_WRITE
    )

    @field_validator("mode")
    @classmethod
    def _known_mode(cls, value: str) -> str:
        from ariadne.loop_module.modes import available_modes

        if value not in available_modes():
            raise ValueError(f"未知 Loop 模式 {value!r}，支持: {available_modes()}")
        return value

    @field_validator("workspace")
    @classmethod
    def _safe_workspace_paths(cls, value: dict[str, str]) -> dict[str, str]:
        """路径穿越在**创建时**就拒绝，不等落盘。

        Worker 侧的 `materialize` 还会再挡一次（请求体不可信，两层都要有），
        但在这里拒绝能给用户一个 422 而不是一个跑起来才失败的 Loop。
        """
        for path in value:
            normalized = path.replace("\\", "/")
            if (
                not path.strip()
                or normalized.startswith("/")
                or ".." in normalized.split("/")
                or ":" in path
            ):
                raise ValueError(f"非法的工作目录路径: {path!r}")
        total = 0
        for path, content in value.items():
            size = len(content.encode("utf-8"))
            if size > MAX_BYTES_PER_FILE:
                raise ValueError(
                    f"种子文件 {path!r} 超过单文件上限 {MAX_BYTES_PER_FILE} 字节"
                )
            total += size
        if total > MAX_TOTAL_BYTES:
            raise ValueError(f"种子文件总量超过 {MAX_TOTAL_BYTES} 字节上限")
        return value


class LoopSummary(BaseModel):
    id: UUID
    project_id: UUID
    mode: str
    state: str
    iteration: int
    cumulative_tokens: int
    cumulative_cost_usd: float
    final_state: str | None
    worker_id: str | None
    goal: dict[str, Any]
    error: str = ""
    created_at: datetime | None = None
    finished_at: datetime | None = None


class CreateLoopResponse(BaseModel):
    loop_id: UUID
    state: str
    stream_url: str
    # Redis 持续不可用时仍保留已落库的 Loop，但明确告诉调用方需要重试。
    queued: bool = True


class ApproveRequest(BaseModel):
    approved: bool
    comment: str = ""


class IterationsResponse(BaseModel):
    iterations: list[dict[str, Any]]


def _to_summary(payload: dict[str, Any]) -> LoopSummary:
    return LoopSummary(
        id=UUID(payload["id"]),
        project_id=UUID(payload["project_id"]),
        mode=payload["mode"],
        state=payload["state"],
        iteration=payload["iteration"],
        cumulative_tokens=payload["cumulative_tokens"],
        cumulative_cost_usd=payload["cumulative_cost_usd"],
        final_state=payload["final_state"],
        worker_id=payload["worker_id"],
        goal=payload["goal"],
        error=payload.get("error", ""),
        created_at=(
            datetime.fromisoformat(payload["created_at"]) if payload.get("created_at") else None
        ),
        finished_at=(
            datetime.fromisoformat(payload["finished_at"]) if payload.get("finished_at") else None
        ),
    )


def _build_goal(body: CreateLoopRequest) -> Goal:
    mode = cast(LoopMode, body.mode)  # validator 已校验已知模式
    return Goal(
        task=body.task,
        assertions=tuple(
            Assertion(
                id=a.id,
                kind=a.kind,
                spec=a.spec,
                weight=a.weight,
                blocking=a.blocking,
                hint=a.hint,
            )
            for a in body.assertions
        ),
        budget=Budget(
            max_iterations=body.budget.max_iterations,
            max_total_tokens=body.budget.max_total_tokens,
            max_cost_usd=body.budget.max_cost_usd,
            max_tokens_per_iteration=body.budget.max_tokens_per_iteration,
            max_wall_clock_seconds=body.budget.max_wall_clock_seconds,
        ),
        mode=mode,
        stall_threshold=body.stall_threshold,
        stall_patience=body.stall_patience,
        workspace=tuple(body.workspace.items()),
    )


async def _enqueue(settings: Settings, loop_id: UUID, project_id: UUID) -> bool:
    """入队交给 Worker 执行。

    短暂队列故障自动重试；最终失败返回 False。Loop 已在 Postgres 落库，
    调用方可据此提示稍后 resume，而不是把未写入 Redis 的消息当成成功。
    带上 project_id：Worker 读 loop_runs 前要先设 RLS 变量。
    """
    import asyncio

    queue = LoopQueue(settings.redis)
    attempts = settings.redis.enqueue_attempts
    try:
        for attempt in range(attempts):
            try:
                await queue.connect()
                await queue.ensure_group()
                await queue.enqueue(str(loop_id), str(project_id))
                return True
            except Exception as exc:
                from ariadne.utils.logging import get_logger

                get_logger(__name__).warning(
                    "loop enqueue attempt failed",
                    extra={
                        "loop_id": str(loop_id),
                        "attempt": attempt + 1,
                        "attempts": attempts,
                        "error": str(exc),
                    },
                )
                if attempt + 1 < attempts:
                    delay = settings.redis.enqueue_backoff_ms * (2**attempt)
                    if delay:
                        await asyncio.sleep(delay / 1000)
        from ariadne.utils.logging import get_logger

        get_logger(__name__).error(
            "loop enqueue exhausted; caller must retry",
            extra={"loop_id": str(loop_id), "attempts": attempts},
        )
        return False
    finally:
        await queue.close()


async def _create_one(
    *, project_id: UUID, body: CreateLoopRequest, pg: Any, settings: Settings
) -> tuple[UUID, bool]:
    goal = _build_goal(body)

    # 目标不可验证 → 422（RFC 9457）。刻意在 API 层提前拒绝：
    # Worker 的 engine 会再验一次（两层校验），但尽早失败体验更好。
    #
    # sandbox_available=True：Worker 会为任何带 COMMAND 断言的 Goal 建工作
    # 目录（见 LoopWorker._prepare_workspace）。此处曾硬编码 False，效果是
    # **所有带 COMMAND 断言的 Loop 在创建时就被 422 拒掉** —— 代码生成场景
    # 是 M3 的标杆场景，却整条路不可用。
    report = validate_goal(
        goal,
        available_metrics=frozenset(),
        sandbox_available=True,
    )
    if not report.ok:
        details = [i.message for i in report.errors]
        raise UnprocessableError(f"目标不可验证: {'; '.join(details)}")

    async with pg.session() as session:
        repo = LoopRunRepository(session)
        loop_id = await repo.create(
            project_id=project_id, mode=body.mode, goal=goal
        )

    queued_result = await _enqueue(settings, loop_id, project_id)
    # 兼容测试/第三方注入的旧式 no-op enqueue（返回 None）：只有显式 False
    # 才表示确认失败。
    return loop_id, queued_result is not False


@router.post(
    "/loops",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CreateLoopResponse,
    summary="创建并启动 Loop（异步）",
)
async def create_loop(
    body: CreateLoopRequest,
    ctx: TenantCtx,
    pg: TenantPg,
    settings: AppSettings,
) -> CreateLoopResponse:
    check_permission(ctx.role, Permission.WRITE)
    loop_id, queued = await _create_one(
        project_id=ctx.project_id, body=body, pg=pg, settings=settings
    )
    return CreateLoopResponse(
        loop_id=loop_id,
        state=LoopState.VALIDATE.value,
        stream_url=f"/v1/loops/{loop_id}/stream",
        queued=queued,
    )


@router.post(
    "/loops/batch",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=list[CreateLoopResponse],
    summary="批量创建 Loop",
)
async def create_loops_batch(
    body: list[CreateLoopRequest],
    ctx: TenantCtx,
    pg: TenantPg,
    settings: AppSettings,
) -> list[CreateLoopResponse]:
    check_permission(ctx.role, Permission.WRITE)
    if len(body) > MAX_BATCH_SIZE:
        raise BadRequestError(f"批量创建上限 {MAX_BATCH_SIZE} 个")
    results: list[CreateLoopResponse] = []
    for req in body:
        loop_id, queued = await _create_one(
            project_id=ctx.project_id, body=req, pg=pg, settings=settings
        )
        results.append(
            CreateLoopResponse(
                loop_id=loop_id,
                state=LoopState.VALIDATE.value,
                stream_url=f"/v1/loops/{loop_id}/stream",
                queued=queued,
            )
        )
    return results


@router.get(
    "/loops", response_model=list[LoopSummary], summary="Loop 列表"
)
async def list_loops(
    ctx: TenantCtx,
    pg: TenantPg,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    state: str | None = None,
    mode: str | None = None,
) -> list[LoopSummary]:
    check_permission(ctx.role, Permission.READ)
    state_enum = None
    if state:
        try:
            state_enum = LoopState(state)
        except ValueError as exc:
            raise BadRequestError(f"未知状态 {state!r}") from exc
    async with pg.session() as session:
        rows = await LoopRunRepository(session).list_recent(
            project_id=ctx.project_id, limit=limit, state=state_enum, mode=mode
        )
    return [_to_summary(to_row_dict(r)) for r in rows]


@router.get(
    "/loops/{loop_id}",
    response_model=LoopSummary,
    summary="Loop 详情",
)
async def get_loop(
    loop_id: UUID, ctx: TenantCtx, pg: TenantPg
) -> LoopSummary:
    check_permission(ctx.role, Permission.READ)
    async with pg.session() as session:
        try:
            row = await LoopRunRepository(session).get(
                project_id=ctx.project_id, loop_id=loop_id
            )
        except LoopRunNotFoundError as exc:
            raise NotFoundError(str(exc), loop_id=str(loop_id)) from exc
    return _to_summary(to_row_dict(row))


@router.get(
    "/loops/{loop_id}/iterations",
    response_model=IterationsResponse,
    summary="Loop 轮次记录",
)
async def loop_iterations(
    loop_id: UUID, ctx: TenantCtx, pg: TenantPg
) -> IterationsResponse:
    """轮次记录从 loop_checkpoints 读（每轮一条不可变快照）。"""
    check_permission(ctx.role, Permission.READ)
    async with pg.session() as session:
        try:
            await LoopRunRepository(session).get(
                project_id=ctx.project_id, loop_id=loop_id
            )
        except LoopRunNotFoundError as exc:
            raise NotFoundError(str(exc), loop_id=str(loop_id)) from exc

        stmt = (
            select(LoopCheckpointRow)
            .where(
                LoopCheckpointRow.loop_id == loop_id,
                LoopCheckpointRow.project_id == ctx.project_id,
            )
            .order_by(LoopCheckpointRow.iteration.asc())
            .limit(200)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        iterations = [
            {
                "iteration": row.iteration,
                "state": row.state,
                "output_fp": row.output_fp,
                "failure_fp": row.failure_fp,
                "cumulative_tokens": row.cumulative_tokens,
                "cumulative_cost_usd": float(row.cumulative_cost_usd),
                "verdict": row.verdict,
                "critique": row.critique,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    return IterationsResponse(iterations=iterations)


@router.post(
    "/loops/{loop_id}/cancel",
    response_model=LoopSummary,
    summary="取消 Loop",
)
async def cancel_loop(
    loop_id: UUID, ctx: TenantCtx, pg: TenantPg
) -> LoopSummary:
    """转 CANCELLED。已终态则冲突。"""
    check_permission(ctx.role, Permission.WRITE)
    async with pg.session() as session:
        repo = LoopRunRepository(session)
        try:
            row = await repo.transition(
                loop_id=loop_id,
                project_id=ctx.project_id,
                to=LoopState.CANCELLED,
            )
        except LoopRunNotFoundError as exc:
            raise NotFoundError(str(exc), loop_id=str(loop_id)) from exc
        except LoopRunConflictError as exc:
            raise ConflictError(str(exc)) from exc
    return _to_summary(to_row_dict(row))


@router.post(
    "/loops/{loop_id}/resume",
    response_model=LoopSummary,
    summary="从最近检查点恢复（重试执行）",
)
async def resume_loop(
    loop_id: UUID,
    ctx: TenantCtx,
    pg: TenantPg,
    settings: AppSettings,
) -> LoopSummary:
    """把状态转回 VALIDATE 并重新入队，由 Worker 从检查点续跑。"""
    check_permission(ctx.role, Permission.WRITE)
    async with pg.session() as session:
        repo = LoopRunRepository(session)
        try:
            row = await repo.transition(
                loop_id=loop_id,
                project_id=ctx.project_id,
                to=LoopState.VALIDATE,
            )
        except LoopRunNotFoundError as exc:
            raise NotFoundError(str(exc), loop_id=str(loop_id)) from exc
        except LoopRunConflictError as exc:
            raise ConflictError(str(exc)) from exc

    await _enqueue(settings, loop_id, ctx.project_id)
    return _to_summary(to_row_dict(row))


@router.post(
    "/loops/{loop_id}/approve",
    response_model=LoopSummary,
    summary="HITL 审批",
)
async def approve_loop(
    loop_id: UUID,
    body: ApproveRequest,
    ctx: TenantCtx,
    pg: TenantPg,
    settings: AppSettings,
) -> LoopSummary:
    """HITL 模式：人工审批推进或拒绝。

    approved=True → 转 EXECUTING 并重新入队（Worker 从 HUMAN_PENDING
    检查点恢复续跑）；False → REJECTED 终止。
    """
    check_permission(ctx.role, Permission.APPROVE)
    target = LoopState.EXECUTING if body.approved else LoopState.REJECTED
    async with pg.session() as session:
        repo = LoopRunRepository(session)
        try:
            row = await repo.transition(
                loop_id=loop_id,
                project_id=ctx.project_id,
                to=target,
                error="" if body.approved else "人工拒绝",
            )
        except LoopRunNotFoundError as exc:
            raise NotFoundError(str(exc), loop_id=str(loop_id)) from exc
        except LoopRunConflictError as exc:
            raise ConflictError(str(exc)) from exc

    if body.approved:
        await _enqueue(settings, loop_id, ctx.project_id)
    return _to_summary(to_row_dict(row))


__all__ = [
    "ApproveRequest",
    "CreateLoopRequest",
    "LoopSummary",
    "router",
]
