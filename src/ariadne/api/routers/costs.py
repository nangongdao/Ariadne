"""成本归因查询。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Final

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ariadne.api.deps import Store, TenantCtx
from ariadne.api.errors import BadRequestError
from ariadne.auth.rbac import Permission, check_permission

router = APIRouter(tags=["costs"])

# 白名单：分组维度直接进 SQL，必须严格限定取值，不能接受任意输入
_ALLOWED_GROUP_BY: Final[frozenset[str]] = frozenset(
    {"provider", "model_request", "minute", "hour", "day"}
)
_TIME_DIMENSIONS: Final[dict[str, str]] = {
    "minute": "minute",
    "hour": "toStartOfHour(minute)",
    "day": "toStartOfDay(minute)",
}

# 桶数量上限。按小时看满 90 天需要 2160 个桶，旧值 500 会把时间序列截掉大半，
# 且截断发生在 ORDER BY cost_usd DESC 之后 —— 丢的是最便宜的那些时段，
# 折线图会缺口后直连成一条不存在的平滑线。
_MAX_BUCKETS: Final[int] = 2400


class CostBucket(BaseModel):
    key: dict[str, str]
    span_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    cost_usd: Decimal


class CostSummary(BaseModel):
    """窗口内的成本汇总。

    总计字段来自不分组、不限量的聚合，与 buckets 是否被截断无关 ——
    对已截断的 buckets 求和会少算总额，而调用方无从察觉。
    """

    from_: datetime
    to: datetime
    total_cost_usd: Decimal
    # input + output + cache_read + cache_write，与 spans 表的 total_tokens
    # 表达式一致。reasoning 不并入：主流 provider 把推理 token 计入
    # output_tokens 计费，再并一次就是双重计数。
    total_tokens: int
    span_count: int
    cache_hit_ratio: float
    # cache_write / reasoning 单独给出：rollup 有这两个维度（审计 P1-9），
    # 缺了它们，带缓存写入或推理 Token 的模型会被系统性低估。
    cache_write_tokens: int
    reasoning_tokens: int
    buckets: list[CostBucket]
    # buckets 是否被 _MAX_BUCKETS 截断。真时 buckets 之和小于 total_cost_usd，
    # 调用方必须据此说明图表只覆盖了一部分，否则用户会拿图去对总额
    truncated: bool = False


@router.get("/costs", response_model=CostSummary, summary="成本归因")
async def get_costs(
    ctx: TenantCtx,
    store: Store,
    group_by: Annotated[
        str, Query(description="逗号分隔：provider,model_request,hour")
    ] = "model_request",
    hours: Annotated[int, Query(ge=1, le=24 * 90)] = 24,
) -> CostSummary:
    check_permission(ctx.role, Permission.VIEW_BILLING)
    dimensions = [d.strip() for d in group_by.split(",") if d.strip()]
    invalid = [d for d in dimensions if d not in _ALLOWED_GROUP_BY]
    if invalid:
        raise BadRequestError(
            f"不支持的分组维度 {invalid}", allowed=sorted(_ALLOWED_GROUP_BY)
        )

    now = datetime.now(UTC)
    since = now - timedelta(hours=hours)

    select_parts: list[str] = []
    group_parts: list[str] = []
    for dim in dimensions:
        expr = _TIME_DIMENSIONS.get(dim, dim)
        select_parts.append(f"toString({expr}) AS {dim}")
        group_parts.append(expr)

    select_clause = ", ".join([*select_parts, ""]) if select_parts else ""
    group_clause = f"GROUP BY {', '.join(group_parts)}" if group_parts else ""

    rows = store.query(
        f"""
        SELECT {select_clause}
            toUInt64(sum(span_count))         AS span_count,
            toUInt64(sum(input_tokens))       AS input_tokens,
            toUInt64(sum(output_tokens))      AS output_tokens,
            toUInt64(sum(cache_read_tokens))  AS cache_read_tokens,
            toUInt64(sum(cache_write_tokens)) AS cache_write_tokens,
            toUInt64(sum(reasoning_tokens))   AS reasoning_tokens,
            sum(cost_usd)                     AS cost_usd
        FROM cost_rollup
        WHERE project_id = {{pid:UUID}} AND minute >= {{since:DateTime}}
        {group_clause}
        ORDER BY cost_usd DESC
        LIMIT {{limit:UInt32}}
        """,
        {
            "pid": ctx.project_id,
            "since": since.replace(tzinfo=None),
            "limit": _MAX_BUCKETS,
        },
        project_id=ctx.project_id,
    )

    buckets = [
        CostBucket(
            key={d: str(row.get(d, "")) for d in dimensions},
            span_count=int(row["span_count"]),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            cache_read_tokens=int(row["cache_read_tokens"]),
            cache_write_tokens=int(row.get("cache_write_tokens") or 0),
            reasoning_tokens=int(row.get("reasoning_tokens") or 0),
            cost_usd=Decimal(str(row["cost_usd"])),
        )
        for row in rows
    ]

    # 总计单独查：不分组、不限量。对 buckets 求和会随分组粒度变化 ——
    # 同一个 30 天窗口按小时和按天会得出不同的"总成本"，用户无法判断哪个可信。
    totals = store.query(
        """
        SELECT
            toUInt64(sum(span_count))         AS span_count,
            toUInt64(sum(input_tokens))       AS input_tokens,
            toUInt64(sum(output_tokens))      AS output_tokens,
            toUInt64(sum(cache_read_tokens))  AS cache_read_tokens,
            toUInt64(sum(cache_write_tokens)) AS cache_write_tokens,
            toUInt64(sum(reasoning_tokens))   AS reasoning_tokens,
            sum(cost_usd)                     AS cost_usd
        FROM cost_rollup
        WHERE project_id = {pid:UUID} AND minute >= {since:DateTime}
        """,
        {"pid": ctx.project_id, "since": since.replace(tzinfo=None)},
        project_id=ctx.project_id,
    )

    # 窗口内无数据时 ClickHouse 仍返回一行，sum 为 NULL
    total_row = totals[0] if totals else {}
    total_input = int(total_row.get("input_tokens") or 0)
    total_output = int(total_row.get("output_tokens") or 0)
    total_cache = int(total_row.get("cache_read_tokens") or 0)
    total_cache_write = int(total_row.get("cache_write_tokens") or 0)
    total_reasoning = int(total_row.get("reasoning_tokens") or 0)

    # 缓存命中率对成本判读很关键：命中率高说明多轮场景的实际计费远低于名义 Token。
    # cache_write 不算"命中"——它是写缓存的计费部分，计入分母会虚高命中率。
    denominator = total_input + total_cache
    return CostSummary(
        from_=since,
        to=now,
        total_cost_usd=Decimal(str(total_row.get("cost_usd") or 0)),
        total_tokens=total_input + total_output + total_cache + total_cache_write,
        span_count=int(total_row.get("span_count") or 0),
        cache_hit_ratio=round(total_cache / denominator, 4) if denominator else 0.0,
        cache_write_tokens=total_cache_write,
        reasoning_tokens=total_reasoning,
        buckets=buckets,
        truncated=len(buckets) >= _MAX_BUCKETS,
    )
