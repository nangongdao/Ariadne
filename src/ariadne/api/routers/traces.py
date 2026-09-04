"""Trace 查询与调用树构建。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ariadne.api.deps import Store, TenantCtx
from ariadne.api.errors import NotFoundError
from ariadne.api.tree import SpanNode, build_tree
from ariadne.auth.rbac import Permission, check_permission

router = APIRouter(tags=["traces"])

_MAX_SPANS_PER_TRACE = 5000


class TraceSummary(BaseModel):
    trace_id: str
    root_name: str
    started_at: datetime
    duration_ms: int
    span_count: int
    error_count: int
    total_tokens: int
    total_cost_usd: Decimal
    models: list[str]


class TraceDetail(BaseModel):
    trace_id: str
    span_count: int
    total_tokens: int
    total_cost_usd: Decimal
    duration_ms: int
    roots: list[SpanNode]
    truncated: bool = False


class SpanListItem(BaseModel):
    trace_id: str
    span_id: str
    name: str
    kind: str
    status: str
    provider: str
    model_request: str
    started_at: datetime
    duration_ms: int
    total_tokens: int
    cost_usd: Decimal


@router.get("/traces", response_model=list[TraceSummary], summary="Trace 列表")
async def list_traces(
    ctx: TenantCtx,
    store: Store,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    before: Annotated[datetime | None, Query()] = None,
    only_errors: Annotated[bool, Query()] = False,
) -> list[TraceSummary]:
    """查 trace_rollup 物化视图而非扫 spans 表。

    游标分页用 started_at（before 参数）而非 OFFSET：深分页时 OFFSET
    会退化为全扫。
    """
    check_permission(ctx.role, Permission.READ)
    where = ["project_id = {pid:UUID}"]
    params: dict[str, Any] = {"pid": ctx.project_id, "limit": limit}
    # 游标条件必须进 HAVING 而不是 WHERE，两个独立的原因：
    # 1. 语法：`started_at` 是 min(started_at) 的别名，别名在同一 SELECT 里优先于
    #    列名，放进 WHERE 会让聚合函数出现在 WHERE 里 → ILLEGAL_AGGREGATION(184)。
    # 2. 语义：游标要筛的是「trace 的起始时刻」即聚合后的 min(started_at)。用 WHERE
    #    筛原始 rollup 行会把跨游标的 trace 截断，它的 duration_ms / span_count 会
    #    只统计到游标之前那部分 —— 分页边界上的 trace 数据是错的，且不报错。
    conditions = []
    if only_errors:
        conditions.append("error_count > 0")
    if before is not None:
        conditions.append("started_at < {before:DateTime64(6)}")
        params["before"] = before
    having = f"HAVING {' AND '.join(conditions)}" if conditions else ""

    rows = store.query(
        f"""
        SELECT
            trace_id,
            any(root_name)                          AS root_name,
            min(started_at)                         AS started_at,
            -- 必须限定表名：别名 `started_at` 在同一 SELECT 里优先于列名，
            -- 写成 min(started_at) 会被解析成 min(min(started_at))，
            -- ClickHouse 报 ILLEGAL_AGGREGATION(184) 让整个端点 500
            toUInt32(dateDiff('millisecond', min(trace_rollup.started_at), max(last_at)))
                                                    AS duration_ms,
            toUInt64(sum(span_count))               AS span_count,
            toUInt64(sum(error_count))              AS error_count,
            toUInt64(sum(total_tokens))             AS total_tokens,
            sum(total_cost_usd)                     AS total_cost_usd,
            arrayFilter(x -> x != '', groupUniqArrayArray(models)) AS models
        FROM trace_rollup
        WHERE {" AND ".join(where)}
        GROUP BY trace_id
        {having}
        ORDER BY started_at DESC
        LIMIT {{limit:UInt32}}
        """,
        params,
        project_id=ctx.project_id,
    )
    return [TraceSummary(**row) for row in rows]


@router.get("/traces/{trace_id}", response_model=TraceDetail, summary="Trace 调用树")
async def get_trace(trace_id: str, ctx: TenantCtx, store: Store) -> TraceDetail:
    check_permission(ctx.role, Permission.READ)
    rows = store.query(
        """
        SELECT
            span_id, parent_span_id, name, kind, operation, status, error_type,
            provider, model_request, model_response, started_at, duration_ms,
            input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
            reasoning_tokens, cost_usd, input_preview, output_preview,
            input_ref, output_ref, attributes, tags, loop_id, iteration
        FROM spans
        WHERE project_id = {pid:UUID} AND trace_id = {tid:String}
        ORDER BY started_at ASC
        LIMIT {limit:UInt32}
        """,
        {"pid": ctx.project_id, "tid": trace_id, "limit": _MAX_SPANS_PER_TRACE + 1},
        project_id=ctx.project_id,
    )
    if not rows:
        raise NotFoundError(f"trace {trace_id} 不存在", trace_id=trace_id)

    truncated = len(rows) > _MAX_SPANS_PER_TRACE
    rows = rows[:_MAX_SPANS_PER_TRACE]

    roots = build_tree(rows)
    total_tokens = sum(
        int(r["input_tokens"]) + int(r["output_tokens"])
        + int(r["cache_read_tokens"]) + int(r["cache_write_tokens"])
        for r in rows
    )
    starts = [r["started_at"] for r in rows]
    span_duration = max(
        (int(r["duration_ms"]) for r in rows if not r["parent_span_id"]), default=0
    )
    wall = int((max(starts) - min(starts)).total_seconds() * 1000) if starts else 0

    return TraceDetail(
        trace_id=trace_id,
        span_count=len(rows),
        total_tokens=total_tokens,
        total_cost_usd=sum((Decimal(str(r["cost_usd"])) for r in rows), Decimal("0")),
        duration_ms=max(span_duration, wall),
        roots=roots,
        truncated=truncated,
    )


@router.get("/spans", response_model=list[SpanListItem], summary="Span 多维过滤")
async def list_spans(
    ctx: TenantCtx,
    store: Store,
    kind: Annotated[str | None, Query()] = None,
    provider: Annotated[str | None, Query()] = None,
    model: Annotated[str | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    min_duration_ms: Annotated[int | None, Query(ge=0)] = None,
    search: Annotated[str | None, Query(min_length=2, max_length=200)] = None,
    since_hours: Annotated[
        int | None,
        Query(ge=1, le=24 * 90, description="只看最近 N 小时，与成本页时间窗对齐"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[SpanListItem]:
    check_permission(ctx.role, Permission.READ)
    where = ["project_id = {pid:UUID}"]
    params: dict[str, Any] = {"pid": ctx.project_id, "limit": limit}

    # 参数化查询，不做字符串拼接，避免注入
    for field, value, ch_type in (
        ("kind", kind, "String"),
        ("provider", provider, "String"),
        ("model_request", model, "String"),
        ("status", status_filter, "String"),
    ):
        if value:
            where.append(f"{field} = {{{field}:{ch_type}}}")
            params[field] = value
    if min_duration_ms is not None:
        where.append("duration_ms >= {min_dur:UInt32}")
        params["min_dur"] = min_duration_ms
    # 成本页下钻带着时间窗过来：不接这个参数的话，"30 天里 gpt-4o 花了 $50"
    # 点进来只会看到最近 100 条，用户会以为筛选没生效
    if since_hours is not None:
        where.append("started_at >= now() - INTERVAL {since_hours:UInt32} HOUR")
        params["since_hours"] = since_hours
    if search:
        where.append(
            "(positionCaseInsensitive(name, {q:String}) > 0"
            " OR positionCaseInsensitive(input_preview, {q:String}) > 0"
            " OR positionCaseInsensitive(output_preview, {q:String}) > 0)"
        )
        params["q"] = search

    rows = store.query(
        f"""
        SELECT trace_id, span_id, name, kind, status, provider, model_request,
               started_at, duration_ms,
               toUInt64(input_tokens + output_tokens
                        + cache_read_tokens + cache_write_tokens) AS total_tokens,
               cost_usd
        FROM spans
        WHERE {" AND ".join(where)}
        ORDER BY started_at DESC
        LIMIT {{limit:UInt32}}
        """,
        params,
        project_id=ctx.project_id,
    )
    return [SpanListItem(**row) for row in rows]
