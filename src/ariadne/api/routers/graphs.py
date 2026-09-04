"""Graph 管理 API 路由。

- POST /v1/graphs — 创建图（校验 + 存储）
- GET /v1/graphs — 列出项目图
- GET /v1/graphs/{id} — 读取单个图
- PUT /v1/graphs/{id} — 更新图（版本化，旧版本 is_active=False）
- DELETE /v1/graphs/{id} — 删除图
- POST /v1/graphs/validate — 仅校验图，不存储
- POST /v1/graphs/{id}/execute — 执行图（GraphExecutor 的生产入口）
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ariadne.api.deps import AppSettings, TenantCtx, TenantPg
from ariadne.api.errors import BadRequestError, ConflictError, NotFoundError, UnprocessableError
from ariadne.auth.rbac import Permission, check_permission
from ariadne.graph_module.models import WorkflowGraph
from ariadne.graph_module.serialize import graph_to_spec, spec_to_graph
from ariadne.graph_module.validate import validate_graph

router = APIRouter(tags=["graphs"])


# ---------- 请求/响应模型 ----------


class GraphCreateRequest(BaseModel):
    """创建/更新图请求。

    graph 字段是 WorkflowGraph 的序列化 dict（nodes/edges/version）。
    name 在项目内唯一。
    """

    name: str = Field(min_length=1, max_length=100)
    graph: dict[str, Any] = Field(...)
    description: str = ""


class GraphValidateRequest(BaseModel):
    """仅校验图请求。"""

    graph: dict[str, Any] = Field(...)


class GraphResponse(BaseModel):
    """图响应。"""

    id: UUID
    project_id: UUID
    name: str
    version: int
    graph: dict[str, Any]
    validation_errors: list[dict[str, Any]] = []
    is_active: bool
    description: str


class ValidationErrorItem(BaseModel):
    """单条校验错误。"""

    field: str
    message: str
    severity: str = "error"


class GraphValidateResponse(BaseModel):
    """校验响应。"""

    ok: bool
    errors: list[ValidationErrorItem] = []
    warnings: list[ValidationErrorItem] = []


class GraphExecuteRequest(BaseModel):
    """执行图请求。

    inputs 传给源节点（无入边的节点）。timeout_s 是墙钟上限：
    图里可能有 llm / loop 节点，不设上限时一个卡住的图会占死一个连接。
    """

    inputs: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float = Field(default=300.0, gt=0, le=1800)


class GraphExecuteResponse(BaseModel):
    """执行结果（异步版本，返回 Job ID）。

    MVP 改造：execute_graph 改为异步提交，立即返回 graph_run_id。
    调用方需轮询 /graphs/runs/{id} 查询状态和结果。
    """

    graph_run_id: UUID
    state: str  # "PENDING"
    status_url: str


class GraphRunStatusResponse(BaseModel):
    """Graph run 状态查询响应（阶段 3：增加 cancelled_at）。"""

    id: UUID
    graph_id: UUID
    state: str
    inputs: dict[str, Any]
    outputs: dict[str, Any] | None
    node_states: dict[str, str]
    errors: list[str]
    created_at: str  # ISO 8601
    finished_at: str | None  # ISO 8601
    cancelled_at: str | None  # ISO 8601 (阶段 3)


class GraphRunListResponse(BaseModel):
    """Graph runs 列表响应。"""

    runs: list[GraphRunStatusResponse]
    total: int


class LangGraphImportRequest(BaseModel):
    """LangGraph 图导入请求（JSON 契约）。

    鸭子类型结构：nodes/edges/branches 对应 StateGraph 内部结构。
    name 非空时导入后直接保存，否则只转换 + 校验。

    branches 格式：{"source_node": {"condition_name": {"ends": {"return_value": "target_node"}}}}
    """

    nodes: dict[str, dict[str, Any]] = Field(
        ..., description="节点字典，key=节点名，value=节点配置（可为空 dict）"
    )
    edges: list[tuple[str, str]] = Field(
        default_factory=list, description="简单边列表 [(源, 目标), ...]"
    )
    branches: dict[str, dict[str, dict[str, Any]]] = Field(
        default_factory=dict,
        description='条件边：{源节点: {条件名: {"ends": {返回值: 目标节点}}}}',
    )
    name: str = Field(default="", max_length=100, description="导入后保存的图名（空则不保存）")
    description: str = ""


# ---------- 端点 ----------


def _deserialize_graph(graph_data: dict[str, Any]) -> WorkflowGraph:
    """从 dict 反序列化 WorkflowGraph。"""
    try:
        return spec_to_graph(graph_data)
    except Exception as exc:
        raise BadRequestError(f"图反序列化失败: {exc}") from exc


def _validate(graph: WorkflowGraph) -> tuple[bool, list[dict[str, Any]], list[dict[str, Any]]]:
    """校验图，返回 (ok, errors, warnings)。"""
    report = validate_graph(graph, sandbox_available=True)
    errors = [
        {"field": i.field, "message": i.message, "severity": i.severity}
        for i in report.issues
        if i.severity == "error"
    ]
    warnings = [
        {"field": i.field, "message": i.message, "severity": i.severity}
        for i in report.issues
        if i.severity == "warning"
    ]
    return report.ok, errors, warnings


@router.post("/graphs", response_model=GraphResponse, status_code=201)
async def create_graph(
    body: GraphCreateRequest,
    ctx: TenantCtx,
    pg: TenantPg,
) -> GraphResponse:
    """创建图：反序列化 + 校验 + 存储。

    不可验证的图返回 422。名称冲突返回 409。
    """
    check_permission(ctx.role, Permission.WRITE)
    import uuid as uuid_mod

    from ariadne.storage.postgres.graph_models import GraphRow

    # 反序列化
    graph = _deserialize_graph(body.graph)

    # 校验
    ok, errors, warnings = _validate(graph)
    if not ok:
        raise UnprocessableError(
            "图校验失败", reasons=[e["message"] for e in errors]
        )

    # 序列化回 dict 用于存储（确保格式一致）
    graph_dict = graph_to_spec(graph)

    async with pg.session() as session:
        # 检查名称唯一性
        from sqlalchemy import select

        existing = await session.execute(
            select(GraphRow).where(
                GraphRow.project_id == ctx.project_id,
                GraphRow.name == body.name,
                GraphRow.is_active.is_(True),
            )
        )
        if existing.scalars().first() is not None:
            raise ConflictError(
                f"图名称 {body.name!r} 已存在", name=body.name
            )

        row = GraphRow(
            id=uuid_mod.uuid4(),
            project_id=ctx.project_id,
            name=body.name,
            version=1,
            graph=graph_dict,
            validation_errors=errors + warnings,
            is_active=True,
            description=body.description,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

        return GraphResponse(
            id=row.id,
            project_id=row.project_id,
            name=row.name,
            version=row.version,
            graph=row.graph,
            validation_errors=row.validation_errors,
            is_active=row.is_active,
            description=row.description,
        )


@router.get("/graphs", response_model=list[GraphResponse])
async def list_graphs(
    ctx: TenantCtx,
    pg: TenantPg,
) -> list[GraphResponse]:
    """列出项目下所有 active 图。"""
    check_permission(ctx.role, Permission.READ)
    from sqlalchemy import select

    from ariadne.storage.postgres.graph_models import GraphRow

    async with pg.session() as session:
        stmt = (
            select(GraphRow)
            .where(
                GraphRow.project_id == ctx.project_id,
                GraphRow.is_active.is_(True),
            )
            .order_by(GraphRow.created_at.desc())
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()
        return [
            GraphResponse(
                id=row.id,
                project_id=row.project_id,
                name=row.name,
                version=row.version,
                graph=row.graph,
                validation_errors=row.validation_errors,
                is_active=row.is_active,
                description=row.description,
            )
            for row in rows
        ]


@router.get("/graphs/runs")
async def list_graph_runs(
    ctx: TenantCtx,
    pg: TenantPg,
    limit: int = 50,
    offset: int = 0,
) -> GraphRunListResponse:
    """列出项目的 graph runs。"""
    check_permission(ctx.role, Permission.READ)

    from ariadne.storage.postgres.repositories.graph_runs import GraphRunRepository

    async with pg.session() as session:
        repo = GraphRunRepository(session)
        runs = await repo.list_runs(ctx.project_id, limit, offset)

    return GraphRunListResponse(
        runs=[
            GraphRunStatusResponse(
                id=r.id,
                graph_id=r.graph_id,
                state=r.state,
                inputs=r.inputs,
                outputs=r.outputs,
                node_states=r.node_states or {},
                errors=r.errors or [],
                created_at=r.created_at.isoformat(),
                finished_at=r.finished_at.isoformat() if r.finished_at else None,
                cancelled_at=r.cancelled_at.isoformat() if r.cancelled_at else None,
            )
            for r in runs
        ],
        total=len(runs),
    )


@router.get("/graphs/runs/{graph_run_id}")
async def get_graph_run_status(
    graph_run_id: UUID,
    ctx: TenantCtx,
    pg: TenantPg,
) -> GraphRunStatusResponse:
    """查询 graph run 状态。"""
    check_permission(ctx.role, Permission.READ)

    from ariadne.storage.postgres.repositories.graph_runs import (
        GraphRunNotFoundError,
        GraphRunRepository,
    )

    async with pg.session() as session:
        repo = GraphRunRepository(session)
        try:
            run = await repo.get(graph_run_id, ctx.project_id)
        except GraphRunNotFoundError as exc:
            raise NotFoundError(f"Graph run {graph_run_id} 不存在") from exc

    return GraphRunStatusResponse(
        id=run.id,
        graph_id=run.graph_id,
        state=run.state,
        inputs=run.inputs,
        outputs=run.outputs,
        node_states=run.node_states or {},
        errors=run.errors or [],
        created_at=run.created_at.isoformat(),
        finished_at=run.finished_at.isoformat() if run.finished_at else None,
        cancelled_at=run.cancelled_at.isoformat() if run.cancelled_at else None,
    )


@router.post("/graphs/runs/{graph_run_id}/cancel", status_code=200)
async def cancel_graph_run(
    graph_run_id: UUID,
    ctx: TenantCtx,
    pg: TenantPg,
) -> dict[str, str]:
    """取消运行中或待执行的 graph run（阶段 3）。

    设置 cancelled_at 标志，Worker 会检测并优雅退出。
    只能取消 PENDING 或 RUNNING 状态的任务。
    """
    check_permission(ctx.role, Permission.WRITE)

    from ariadne.storage.postgres.repositories.graph_runs import (
        GraphRunNotFoundError,
        GraphRunRepository,
    )

    async with pg.session() as session:
        repo = GraphRunRepository(session)

        # 验证任务存在且属于当前项目
        try:
            await repo.get(graph_run_id, ctx.project_id)
        except GraphRunNotFoundError as exc:
            raise NotFoundError(f"Graph run {graph_run_id} 不存在") from exc

        # 标记取消
        success = await repo.cancel(graph_run_id)
        await session.commit()

        if not success:
            raise BadRequestError(
                "无法取消该任务，可能已完成或已被取消",
                graph_run_id=str(graph_run_id),
            )

    return {"message": "任务已标记为取消，Worker 会优雅退出"}


@router.get(
    "/graphs/runs/{graph_run_id}/stream",
    summary="Graph 实时事件流（SSE）",
)
async def stream_graph_run_events(
    graph_run_id: UUID,
    ctx: TenantCtx,
    pg: TenantPg,
    request: Request,
) -> StreamingResponse:
    """SSE 实时推送 Graph run 状态变化（阶段 3-3）。

    返回 `text/event-stream`。前端用 EventSource 订阅。
    事件：run_started → 逐节点 node_state（completed/failed/skipped）
    → run_finished（COMPLETED/FAILED/CANCELLED）。
    断线重连后从 GET /graphs/runs/{id} 拉当前状态，不做事件重放。
    """
    check_permission(ctx.role, Permission.READ)

    from ariadne.storage.postgres.repositories.graph_runs import (
        GraphRunNotFoundError,
        GraphRunRepository,
    )

    # 校验 run 存在（404 提前返回，而非开一个永不结束的流）
    async with pg.session() as session:
        repo = GraphRunRepository(session)
        try:
            await repo.get(graph_run_id, ctx.project_id)
        except GraphRunNotFoundError as exc:
            raise NotFoundError(f"Graph run {graph_run_id} 不存在") from exc

    settings: AppSettings = request.app.state.settings

    async def generate() -> Any:
        from ariadne.api.sse import event_stream

        channel = f"graph:events:{graph_run_id}"
        async for frame in event_stream(settings.redis.url, channel):
            yield frame

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 关掉 nginx 缓冲，保证实时
        },
    )


@router.get("/graphs/{graph_id}", response_model=GraphResponse)
async def get_graph(
    graph_id: UUID,
    ctx: TenantCtx,
    pg: TenantPg,
) -> GraphResponse:
    """读取单个图。"""
    check_permission(ctx.role, Permission.READ)
    from sqlalchemy import select

    from ariadne.storage.postgres.graph_models import GraphRow

    async with pg.session() as session:
        stmt = select(GraphRow).where(
            GraphRow.id == graph_id,
            GraphRow.project_id == ctx.project_id,
        )
        result = await session.execute(stmt)
        row = result.scalars().first()

        if row is None:
            raise NotFoundError(f"图 {graph_id} 不存在", graph_id=str(graph_id))

        return GraphResponse(
            id=row.id,
            project_id=row.project_id,
            name=row.name,
            version=row.version,
            graph=row.graph,
            validation_errors=row.validation_errors,
            is_active=row.is_active,
            description=row.description,
        )


@router.put("/graphs/{graph_id}", response_model=GraphResponse)
async def update_graph(
    graph_id: UUID,
    body: GraphCreateRequest,
    ctx: TenantCtx,
    pg: TenantPg,
) -> GraphResponse:
    """更新图：版本化更新，旧版本 is_active=False，新版本 is_active=True。"""
    check_permission(ctx.role, Permission.WRITE)
    import uuid as uuid_mod

    from sqlalchemy import select

    from ariadne.storage.postgres.graph_models import GraphRow

    # 反序列化 + 校验
    graph = _deserialize_graph(body.graph)
    ok, errors, warnings = _validate(graph)
    if not ok:
        raise UnprocessableError(
            "图校验失败", reasons=[e["message"] for e in errors]
        )

    graph_dict = graph_to_spec(graph)

    async with pg.session() as session:
        stmt = select(GraphRow).where(
            GraphRow.id == graph_id,
            GraphRow.project_id == ctx.project_id,
            GraphRow.is_active.is_(True),
        )
        result = await session.execute(stmt)
        existing = result.scalars().first()

        if existing is None:
            raise NotFoundError(f"图 {graph_id} 不存在", graph_id=str(graph_id))

        # 改名撞别的图要和创建一样拒掉。数据库上没有唯一索引，不查就会留下两行
        # 同名的活跃图 —— 列表里两行看着一模一样，按名字找图也不知道该给哪个。
        # 排除自己：每个版本是一行新 uuid，这里的 graph_id 就是将被失活的那行
        if body.name != existing.name:
            clash = await session.execute(
                select(GraphRow).where(
                    GraphRow.project_id == ctx.project_id,
                    GraphRow.name == body.name,
                    GraphRow.is_active.is_(True),
                    GraphRow.id != graph_id,
                )
            )
            if clash.scalars().first() is not None:
                raise ConflictError(f"图名称 {body.name!r} 已存在", name=body.name)

        # 旧版本失活
        existing.is_active = False

        # 创建新版本
        new_version = existing.version + 1
        new_row = GraphRow(
            id=uuid_mod.uuid4(),
            project_id=ctx.project_id,
            name=body.name,
            version=new_version,
            graph=graph_dict,
            validation_errors=errors + warnings,
            is_active=True,
            description=body.description,
        )
        session.add(new_row)
        await session.commit()
        await session.refresh(new_row)

        return GraphResponse(
            id=new_row.id,
            project_id=new_row.project_id,
            name=new_row.name,
            version=new_row.version,
            graph=new_row.graph,
            validation_errors=new_row.validation_errors,
            is_active=new_row.is_active,
            description=new_row.description,
        )


@router.delete("/graphs/{graph_id}", status_code=204)
async def delete_graph(
    graph_id: UUID,
    ctx: TenantCtx,
    pg: TenantPg,
) -> None:
    """删除图（软删除：is_active=False）。"""
    check_permission(ctx.role, Permission.WRITE)
    from sqlalchemy import select

    from ariadne.storage.postgres.graph_models import GraphRow

    async with pg.session() as session:
        stmt = select(GraphRow).where(
            GraphRow.id == graph_id,
            GraphRow.project_id == ctx.project_id,
            GraphRow.is_active.is_(True),
        )
        result = await session.execute(stmt)
        row = result.scalars().first()

        if row is None:
            raise NotFoundError(f"图 {graph_id} 不存在", graph_id=str(graph_id))

        row.is_active = False
        await session.commit()


@router.post("/graphs/validate", response_model=GraphValidateResponse)
async def validate_graph_endpoint(
    body: GraphValidateRequest,
    ctx: TenantCtx,
    pg: TenantPg,
) -> GraphValidateResponse:
    """仅校验图，不存储。返回所有 error 和 warning。"""
    check_permission(ctx.role, Permission.WRITE)
    graph = _deserialize_graph(body.graph)
    ok, errors, warnings = _validate(graph)
    return GraphValidateResponse(
        ok=ok,
        errors=[ValidationErrorItem(**e) for e in errors],
        warnings=[ValidationErrorItem(**w) for w in warnings],
    )


@router.post("/graphs/{graph_id}/execute", status_code=202)
async def execute_graph(
    graph_id: UUID,
    body: GraphExecuteRequest,
    ctx: TenantCtx,
    pg: TenantPg,
    settings: AppSettings,
) -> GraphExecuteResponse:
    """执行图（异步提交，返回 Job ID）。

    阶段 2 改造：
    1. 创建 graph_run 记录（PENDING）
    2. 入队到 Redis Streams（Worker 消费）
    3. 立即返回 graph_run_id + status_url

    调用方需轮询 GET /graphs/runs/{graph_run_id} 查询状态和结果。

    破坏性变更：从同步执行（200 OK + 结果）改为异步提交（202 Accepted + Job ID）。
    """
    check_permission(ctx.role, Permission.WRITE)

    from ariadne.storage.postgres.repositories.graph_runs import GraphRunRepository
    from ariadne.storage.postgres.repositories.graphs import GraphNotFoundError, GraphRepository

    # 1. 验证图存在且可执行
    async with pg.session() as session:
        graphs_repo = GraphRepository(session)
        try:
            graph_row = await graphs_repo.get(ctx.project_id, graph_id)
        except GraphNotFoundError as exc:
            raise NotFoundError(f"图 {graph_id} 不存在", graph_id=str(graph_id)) from exc

    graph = _deserialize_graph(graph_row.graph)
    ok, errors, _warnings = _validate(graph)
    if not ok:
        raise UnprocessableError(
            "图校验失败，拒绝执行", reasons=[e["message"] for e in errors]
        )

    # 2. 创建 graph_run 记录
    async with pg.session() as session:
        graph_runs_repo = GraphRunRepository(session)
        graph_run_id = await graph_runs_repo.create(
            project_id=ctx.project_id,
            graph_id=graph_id,
            inputs=body.inputs,
        )
        await session.commit()

    # 3. 入队到 Redis（或测试模式下同步执行）
    if settings.env == "test":
        # 测试模式：使用 MVP Worker 直接同步执行
        from ariadne.worker.graph_worker_singleton import get_graph_worker
        worker = get_graph_worker()
        await worker._execute(graph_run_id, ctx.project_id)
    else:
        # 生产环境：入队到 Redis Streams（阶段 2）
        from ariadne.worker.graph_queue_singleton import get_graph_queue
        queue = get_graph_queue()
        await queue.enqueue(str(graph_run_id), str(ctx.project_id))

    # 4. 返回 Job ID
    return GraphExecuteResponse(
        graph_run_id=graph_run_id,
        state="PENDING",
        status_url=f"/v1/graphs/runs/{graph_run_id}",
    )


@router.post("/graphs/import-langgraph", response_model=GraphResponse, status_code=201)
async def import_langgraph_endpoint(
    body: LangGraphImportRequest,
    ctx: TenantCtx,
    pg: TenantPg,
) -> GraphResponse:
    """导入 LangGraph 图定义。

    将 LangGraph StateGraph 结构转换为 Ariadne WorkflowGraph。
    name 非空时校验后直接保存；为空时仅转换 + 校验后返回，不持久化。
    """
    check_permission(ctx.role, Permission.WRITE)
    import uuid as uuid_mod

    from ariadne.graph_module.importers.langgraph import import_langgraph
    from ariadne.storage.postgres.graph_models import GraphRow

    # 构造鸭子类型对象（模拟 StateGraph 结构）
    class _BranchSpec:
        """模拟 langgraph BranchSpec，提供 .ends 属性。"""
        def __init__(self, ends: dict[str, str] | None):
            self.ends = ends

    class _DuckGraph:
        def __init__(
            self,
            nodes_dict: dict[str, Any],
            edges_list: list[tuple[str, str]],
            branches_dict: dict[str, Any],
        ):
            self.nodes = nodes_dict
            self.edges = {tuple(e) for e in edges_list}
            # 将 branches JSON 转换为带 .ends 的对象
            # 期望格式：{"source": {"cond_name": {"ends": {...}}}}
            self.branches: dict[str, dict[str, _BranchSpec]] = {}
            for source, cond_dict in branches_dict.items():
                self.branches[source] = {}
                for cond_name, cond_data in cond_dict.items():
                    ends = cond_data.get("ends") if isinstance(cond_data, dict) else None
                    self.branches[source][cond_name] = _BranchSpec(ends)

    duck = _DuckGraph(body.nodes, body.edges, body.branches)

    # 导入转换
    try:
        graph = import_langgraph(duck)
    except Exception as exc:
        raise BadRequestError(f"LangGraph 导入失败: {exc}") from exc

    # 校验
    ok, errors, warnings = _validate(graph)
    if not ok:
        raise UnprocessableError(
            "导入的图校验失败", reasons=[e["message"] for e in errors]
        )

    graph_dict = graph_to_spec(graph)

    # name 为空则只返回转换结果，不保存
    if not body.name:
        return GraphResponse(
            id=uuid_mod.uuid4(),
            project_id=ctx.project_id,
            name="<未保存>",
            version=0,
            graph=graph_dict,
            validation_errors=errors + warnings,
            is_active=False,
            description=body.description,
        )

    # 保存流程与 create_graph 一致
    async with pg.session() as session:
        from sqlalchemy import select

        existing = await session.execute(
            select(GraphRow).where(
                GraphRow.project_id == ctx.project_id,
                GraphRow.name == body.name,
                GraphRow.is_active.is_(True),
            )
        )
        if existing.scalars().first() is not None:
            raise ConflictError(
                f"图名称 {body.name!r} 已存在", name=body.name
            )

        row = GraphRow(
            id=uuid_mod.uuid4(),
            project_id=ctx.project_id,
            name=body.name,
            version=1,
            graph=graph_dict,
            validation_errors=errors + warnings,
            is_active=True,
            description=body.description,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

        return GraphResponse(
            id=row.id,
            project_id=row.project_id,
            name=row.name,
            version=row.version,
            graph=row.graph,
            validation_errors=row.validation_errors,
            is_active=row.is_active,
            description=row.description,
        )


__all__ = ["router"]
