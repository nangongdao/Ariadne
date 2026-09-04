"""规则管理 + 试跑 API 路由。

- GET /v1/rules — 列出当前项目规则集
- PUT /v1/rules — 更新规则集
- POST /v1/rules/test — 试跑：给定 hook + context，返回 Decision
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ariadne.api.deps import TenantCtx, TenantPg
from ariadne.api.errors import BadRequestError
from ariadne.auth.rbac import Permission, check_permission
from ariadne.harness_module.evaluator import EvaluationError
from ariadne.harness_module.loader import RuleLoadError, compile_rule_set
from ariadne.harness_module.models import (
    Action,
    HarnessContext,
    HookKind,
    Rule,
    RuleCategory,
    Severity,
)
from ariadne.storage.postgres.harness_models import RuleSetRow

router = APIRouter(tags=["rules"])


# ---------- 请求/响应模型 ----------


class RuleItem(BaseModel):
    """单条规则的 API 表示。"""

    id: str = Field(min_length=1)
    category: str  # input / output / resource / tool
    hook: str  # pre_model / post_model / pre_tool / post_tool / pre_persist
    when: str = Field(min_length=1)
    action: str = "warn"
    severity: str = "medium"
    message: str = ""
    rewrite_strategy: str = ""
    route_target: str = ""


class RuleSetUpdateRequest(BaseModel):
    """更新规则集请求。"""

    name: str = Field(min_length=1)
    rules: list[RuleItem] = Field(min_length=0)


class RuleSetResponse(BaseModel):
    """规则集响应。"""

    id: UUID
    project_id: UUID
    name: str
    version: int
    rules: list[dict[str, Any]]
    is_active: bool


class RuleTestRequest(BaseModel):
    """试跑请求。"""

    hook: str  # pre_model / post_model / ...
    context: dict[str, Any] = Field(default_factory=dict)


class RuleHitResponse(BaseModel):
    rule_id: str
    category: str
    action: str
    severity: str
    message: str


class RuleTestResponse(BaseModel):
    """试跑响应。返回裁决（命中规则 + 胜出动作）。"""

    action: str
    winning_hit: RuleHitResponse | None = None
    hits: list[RuleHitResponse] = []
    message: str = ""


# ---------- 端点 ----------


@router.get("/rules", response_model=list[RuleSetResponse])
async def list_rule_sets(
    ctx: TenantCtx,
    pg: TenantPg,
) -> list[RuleSetResponse]:
    """列出项目下所有规则集。"""
    check_permission(ctx.role, Permission.READ)
    from sqlalchemy import select

    async with pg.session() as session:
        stmt = (
            select(RuleSetRow)
            .where(RuleSetRow.project_id == ctx.project_id)
            .order_by(RuleSetRow.created_at.desc())
        )
        result = await session.execute(stmt)
        rows = result.scalars().all()
        return [
            RuleSetResponse(
                id=row.id,
                project_id=row.project_id,
                name=row.name,
                version=row.version,
                rules=row.rules,
                is_active=row.is_active,
            )
            for row in rows
        ]


@router.put("/rules", response_model=RuleSetResponse, status_code=201)
async def update_rule_set(
    body: RuleSetUpdateRequest,
    ctx: TenantCtx,
    pg: TenantPg,
) -> RuleSetResponse:
    """创建或更新规则集。每次更新创建新版本。"""
    check_permission(ctx.role, Permission.MANAGE_RULES)
    import uuid as uuid_mod

    from sqlalchemy import select

    rules_data = [r.model_dump() for r in body.rules]

    # 校验规则集可编译（fail-fast）
    try:
        rule_objs = [_to_rule(r) for r in rules_data]
        compile_rule_set(rule_objs)
    except (RuleLoadError, EvaluationError) as exc:
        raise BadRequestError(f"规则集校验失败: {exc}") from exc

    async with pg.session() as session:
        # 查找是否已有同名规则集
        stmt = (
            select(RuleSetRow)
            .where(
                RuleSetRow.project_id == ctx.project_id,
                RuleSetRow.name == body.name,
                RuleSetRow.is_active.is_(True),
            )
            .order_by(RuleSetRow.version.desc())
        )
        result = await session.execute(stmt)
        existing = result.scalars().first()

        if existing:
            # 旧版本设为 inactive
            existing.is_active = False
            new_version = existing.version + 1
        else:
            new_version = 1

        row = RuleSetRow(
            id=uuid_mod.uuid4(),
            project_id=ctx.project_id,
            name=body.name,
            version=new_version,
            rules=rules_data,
            is_active=True,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)

        return RuleSetResponse(
            id=row.id,
            project_id=row.project_id,
            name=row.name,
            version=row.version,
            rules=row.rules,
            is_active=row.is_active,
        )


@router.post("/rules/test", response_model=RuleTestResponse)
async def test_rules(
    body: RuleTestRequest,
    ctx: TenantCtx,
    pg: TenantPg,
) -> RuleTestResponse:
    """试跑规则集：给定 hook + context，返回裁决。"""
    check_permission(ctx.role, Permission.READ)
    from sqlalchemy import select

    # 先验证 hook（即使无规则集也应拒绝未知 hook）
    try:
        hook = HookKind(body.hook)
    except ValueError as exc:
        raise BadRequestError(f"未知 hook: {body.hook}") from exc

    async with pg.session() as session:
        stmt = (
            select(RuleSetRow)
            .where(
                RuleSetRow.project_id == ctx.project_id,
                RuleSetRow.is_active.is_(True),
            )
            .order_by(RuleSetRow.version.desc())
        )
        result = await session.execute(stmt)
        row = result.scalars().first()

        if row is None:
            # 无规则集 → 全 ALLOW
            return RuleTestResponse(action="allow")

        # 编译规则集
        try:
            evaluator = compile_rule_set([_to_rule(r) for r in row.rules])
        except (RuleLoadError, EvaluationError) as exc:
            raise BadRequestError(f"规则集编译失败: {exc}") from exc

        hctx = HarnessContext(hook=hook, **_filter_context(body.hook, body.context))
        decision = evaluator.evaluate(hook=hook, context=hctx)

        winning = None
        if decision.winning_hit:
            winning = RuleHitResponse(
                rule_id=decision.winning_hit.rule.id,
                category=decision.winning_hit.rule.category.value,
                action=decision.winning_hit.rule.action.value,
                severity=decision.winning_hit.rule.severity.value,
                message=decision.winning_hit.message,
            )

        hits = [
            RuleHitResponse(
                rule_id=h.rule.id,
                category=h.rule.category.value,
                action=h.rule.action.value,
                severity=h.rule.severity.value,
                message=h.message,
            )
            for h in decision.hits
        ]

        return RuleTestResponse(
            action=decision.action.value,
            winning_hit=winning,
            hits=hits,
            message=decision.message,
        )


def _to_rule(data: dict[str, Any]) -> Rule:
    """从 dict（JSON）构造 Rule，将字符串字段转为对应枚举。

    Rule 是 frozen dataclass 不是 Pydantic 模型，不会自动转换类型；
    若直接传字符串，evaluator 的 `cr.rule.hook is hook` 身份比较会失败。
    """
    return Rule(
        id=data["id"],
        category=RuleCategory(data["category"]),
        hook=HookKind(data["hook"]),
        when=data["when"],
        action=Action(data.get("action", "warn")),
        severity=Severity(data.get("severity", "medium")),
        message=data.get("message", ""),
        rewrite_strategy=data.get("rewrite_strategy", ""),
        route_target=data.get("route_target", ""),
    )


def _filter_context(hook: str, context: dict[str, Any]) -> dict[str, Any]:
    """根据 hook 过滤上下文字段，只保留该卡点可用的字段。"""
    allowed: dict[str, set[str]] = {
        "pre_model": {"input", "loop"},
        "post_model": {"output", "usage", "cost", "loop"},
        "pre_tool": {"tool", "loop"},
        "post_tool": {"tool", "artifact", "loop"},
        "pre_persist": {"artifact", "loop"},
    }
    allowed_keys = allowed.get(hook, set())
    return {k: v for k, v in context.items() if k in allowed_keys}


__all__ = ["router"]
