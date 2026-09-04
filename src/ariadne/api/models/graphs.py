"""Graph API 的请求/响应模型。

从 routers/graphs.py 拆出（2026-09-04）：响应模型独立成模块，让路由
文件聚焦端点逻辑，符合 800 行文件上限约定。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


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


__all__ = [
    "GraphCreateRequest",
    "GraphExecuteRequest",
    "GraphExecuteResponse",
    "GraphResponse",
    "GraphRunListResponse",
    "GraphRunStatusResponse",
    "GraphValidateRequest",
    "GraphValidateResponse",
    "LangGraphImportRequest",
    "ValidationErrorItem",
]