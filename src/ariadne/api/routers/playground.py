"""Playground REST API —— 客户端请求组装器（不是服务端 LLM 执行器）。

**定位**：快速调试工具，用于对比不同配置、从历史 Span 复现问题、固化配置为 spec.yaml。

**关键设计**：
- API 不调用 LLM：只返回组装好的请求配置，由客户端用自己的 API Key 执行
- 平台不持有密钥：所有 Provider 调用在客户端完成
- 单次调用：不做迭代收敛（迭代由 Loop API 负责）

调试闭环的关键环节（docs/M5-spec §4.4）：

1. 多配置并排跑 —— 同一 prompt 用不同 model/temperature 跑，对比输出
2. 一键固化为 spec —— 调好的配置直接生成 spec.yaml，进版本控制
3. 从历史 span 复现 —— 从任意 span 提取输入，用原始参数复现问题

与 Loop API 的区别：Playground 不做迭代收敛，是单次 LLM 调用的
快速实验台。调好后"固化为 spec"或"以此输入创建 Loop"才进入 Loop 流程。

如需服务端执行能力（Job 管理、成本/Trace 落库、取消/重试），应使用
Loop API 或 Graph 执行端点。
"""

from __future__ import annotations

from typing import Any

import yaml
from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from ariadne.api.deps import Store, TenantCtx
from ariadne.api.errors import BadRequestError, NotFoundError
from ariadne.auth.rbac import Permission, check_permission

router = APIRouter(tags=["playground"])

# 单次对比的配置上限。太多会让请求过重，且对比视图没法展示。
MAX_COMPARE_CONFIGS = 8


class LLMConfig(BaseModel):
    """单次 LLM 调用配置。"""

    model: str = Field(default="gpt-4o", min_length=1, max_length=100)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=1, le=200_000)
    system_prompt: str = Field(default="", max_length=10000)


class PlaygroundRunRequest(BaseModel):
    """单次 Playground 运行请求。"""

    prompt: str = Field(min_length=1, max_length=50000)
    config: LLMConfig = Field(default_factory=LLMConfig)


class PlaygroundRunResponse(BaseModel):
    """单次运行结果（客户端执行）。

    **重要**：API 只返回组装好的请求配置，不调用 LLM。
    客户端需用返回的 `config` + `prompt` 调用 Provider，
    平台不持有用户的 API Key。

    **字段说明**：
    - `request_id`: 本次请求的唯一标识（用于日志关联）
    - `prompt`: 输入文本
    - `config`: LLM 配置（model/temperature/max_tokens/system_prompt）
    - `estimated_cost_usd`: 预估成本（基于 token 计价表，实际成本以 Provider 账单为准）
    """

    request_id: str
    prompt: str
    config: LLMConfig
    # 客户端用这些参数调 provider 后回填 output
    estimated_cost_usd: float = 0.0


class PlaygroundCompareRequest(BaseModel):
    """多配置对比请求。"""

    prompt: str = Field(min_length=1, max_length=50000)
    configs: list[LLMConfig] = Field(min_length=1, max_length=MAX_COMPARE_CONFIGS)


class ConfigResult(BaseModel):
    """单个配置的运行结果。"""

    config: LLMConfig
    output: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str = ""


class PlaygroundCompareResponse(BaseModel):
    """对比结果（客户端执行）。

    **重要**：每个 `ConfigResult` 的 `output` 字段初始为空，
    需由客户端并行调用各 Provider 后回填。

    **工作流**：
    1. API 返回每个配置的占位结果
    2. 客户端并行调用 OpenAI/Anthropic/其他 Provider
    3. 客户端将每个响应填入对应 `ConfigResult.output`
    4. 客户端展示对比视图

    **字段说明**：
    - `results`: 按 `configs` 顺序排列的结果列表
    - 每个结果的 `output`/`input_tokens`/`output_tokens`/`cost_usd` 需客户端回填
    """

    request_id: str
    prompt: str
    results: list[ConfigResult]


class FreezeSpecRequest(BaseModel):
    """将 Playground 配置固化为 spec.yaml。"""

    task: str = Field(min_length=1, max_length=2000)
    model: str = Field(min_length=1, max_length=100)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    # 可选断言（固化时用户可以补充）
    assertions: list[dict[str, Any]] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)


class FreezeSpecResponse(BaseModel):
    """生成的 spec.yaml 内容。"""

    spec_yaml: str
    spec_dict: dict[str, Any]


class ReproduceRequest(BaseModel):
    """从历史 span 复现。"""

    trace_id: str = Field(min_length=1)
    span_id: str = Field(min_length=1)


class ReproduceResponse(BaseModel):
    """从 span 提取的复现信息。

    包含原始输入（prompt）、原始配置（model/temperature），
    让用户在 Playground 中用相同参数重跑。
    """

    trace_id: str
    span_id: str
    prompt: str
    config: LLMConfig
    original_output: str
    original_cost_usd: float


@router.post(
    "/playground/run",
    response_model=PlaygroundRunResponse,
    status_code=status.HTTP_200_OK,
    summary="单次 Playground 运行",
)
async def playground_run(
    request: PlaygroundRunRequest,
    ctx: TenantCtx,
) -> PlaygroundRunResponse:
    """组装单次运行请求（客户端执行）。

    **重要**：API 不调用 LLM。返回的 `config` + `prompt` 需由客户端
    用自己的 Provider API Key 执行。平台不持有或存储用户密钥。

    **工作流**：
    1. 客户端调用此端点获取组装好的请求配置
    2. 客户端用返回的参数调用 OpenAI/Anthropic/其他 Provider
    3. 客户端在前端展示结果或用结果创建 Loop/固化为 Spec

    **用途**：快速调试单个配置，无状态、无持久化。
    """
    check_permission(ctx.role, Permission.WRITE)
    import uuid

    return PlaygroundRunResponse(
        request_id=str(uuid.uuid4()),
        prompt=request.prompt,
        config=request.config,
    )


@router.post(
    "/playground/compare",
    response_model=PlaygroundCompareResponse,
    status_code=status.HTTP_200_OK,
    summary="多配置并排对比",
)
async def playground_compare(
    request: PlaygroundCompareRequest,
    ctx: TenantCtx,
) -> PlaygroundCompareResponse:
    """多配置并排对比（客户端执行）。

    **重要**：API 不调用 LLM。返回占位结果，每个 `ConfigResult` 的
    `output` 字段为空，需由客户端并行调用各 Provider 后回填。

    **工作流**：
    1. 客户端调用此端点，传入多个配置（最多 8 个）
    2. API 返回每个配置的占位结果（`output` 为空）
    3. 客户端并行调用各 Provider，获取每个配置的输出
    4. 客户端在前端对比展示结果

    **用途**：A/B 测试不同 model/temperature 组合的效果。
    """
    check_permission(ctx.role, Permission.WRITE)
    import uuid

    return PlaygroundCompareResponse(
        request_id=str(uuid.uuid4()),
        prompt=request.prompt,
        results=[ConfigResult(config=c) for c in request.configs],
    )


@router.post(
    "/playground/freeze",
    response_model=FreezeSpecResponse,
    status_code=status.HTTP_200_OK,
    summary="固化为 spec.yaml",
)
async def playground_freeze(
    request: FreezeSpecRequest,
    ctx: TenantCtx,
) -> FreezeSpecResponse:
    """将 Playground 配置固化为 spec.yaml。

    生成的 spec 可直接进版本控制，或用 ariadne CLI 启动 Loop。
    model/temperature 存在 goal 的额外 metadata 中（M5 扩展），
    因为 GoalSpec 原生不带 model 字段 —— model 由 loop mode 决定。
    """
    check_permission(ctx.role, Permission.WRITE)
    from ariadne.spec_module.schema import (
        AssertionSpec,
        BudgetSpec,
        GoalSpec,
        Spec,
    )

    # 构造断言 —— Playground 调试阶段可能无断言，GoalSpec 要求至少 1 条，
    # 注入一个占位 regex 断言（用户后续在 spec 中替换为真实断言）
    assertions = [AssertionSpec(**a) for a in request.assertions]
    if not assertions:
        assertions = [
            AssertionSpec(
                id="placeholder",
                kind="regex",
                spec={"pattern": "."},
                blocking=False,
                hint="Playground 固化时的占位断言，请替换为真实断言",
            )
        ]

    # 构造预算（用默认值 + 用户覆盖）
    budget_defaults = {
        "max_iterations": 10,
        "max_total_tokens": 200_000,
        "max_cost_usd": 1.0,
        "max_tokens_per_iteration": 32_000,
        "max_wall_clock_seconds": 900,
    }
    budget_defaults.update(request.budget)
    budget = BudgetSpec(**budget_defaults)

    spec = Spec(
        goal=GoalSpec(
            task=request.task,
            assertions=assertions,
            budget=budget,
        )
    )
    spec_dict = spec.model_dump()
    # Playground 的 model/temperature 作为额外 metadata 附加到 goal
    spec_dict["goal"]["playground_model"] = request.model
    spec_dict["goal"]["playground_temperature"] = request.temperature
    spec_yaml = yaml.dump(spec_dict, allow_unicode=True, sort_keys=False)
    return FreezeSpecResponse(spec_yaml=spec_yaml, spec_dict=spec_dict)


@router.post(
    "/playground/reproduce",
    response_model=ReproduceResponse,
    status_code=status.HTTP_200_OK,
    summary="从历史 span 复现",
)
async def playground_reproduce(
    request: ReproduceRequest,
    ctx: TenantCtx,
    store: Store,
) -> ReproduceResponse:
    """从历史 span 提取输入和配置，用于 Playground 复现。

    查 ClickHouse 的 spans 表，取出原始 input_preview + provider/model 信息。
    """
    check_permission(ctx.role, Permission.WRITE)
    rows = store.query(
        """
        SELECT
            span_id, name, kind, provider, model_request, model_response,
            input_preview, output_preview, cost_usd, attributes
        FROM spans
        WHERE project_id = {pid:UUID}
          AND trace_id = {tid:String}
          AND span_id = {sid:String}
        LIMIT 1
        """,
        {
            "pid": ctx.project_id,
            "tid": request.trace_id,
            "sid": request.span_id,
        },
    )
    if not rows:
        raise NotFoundError(
            f"span {request.span_id} 不存在",
            trace_id=request.trace_id,
            span_id=request.span_id,
        )

    row = rows[0]
    input_text = row.get("input_preview") or ""
    output_text = row.get("output_preview") or ""
    model = row.get("model_request") or row.get("model_response") or "gpt-4"

    # 从 attributes 提取 temperature（如果有）
    attrs = row.get("attributes") or {}
    if isinstance(attrs, str):
        import json

        try:
            attrs = json.loads(attrs)
        except Exception:
            attrs = {}

    temperature = 0.7
    if isinstance(attrs, dict):
        temp_val = attrs.get("temperature") or attrs.get("gen_ai.temperature")
        if isinstance(temp_val, (int, float)):
            temperature = float(temp_val)

    cost = float(row.get("cost_usd") or 0.0)

    if not input_text:
        raise BadRequestError(
            f"span {request.span_id} 无输入预览，无法复现",
            span_id=request.span_id,
        )

    return ReproduceResponse(
        trace_id=request.trace_id,
        span_id=request.span_id,
        prompt=input_text,
        config=LLMConfig(
            model=str(model),
            temperature=temperature,
        ),
        original_output=output_text,
        original_cost_usd=cost,
    )


__all__ = ["router"]
