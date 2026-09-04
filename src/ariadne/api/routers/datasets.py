"""数据集 API。

版本不可变是硬约束：没有 PUT/PATCH 端点，改内容只能 POST 新版本。
这不是遗漏 —— 允许原地改会让历史实验的 content_hash 失效。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from ariadne.api.deps import TenantCtx, TenantPg
from ariadne.api.errors import BadRequestError, ConflictError, NotFoundError
from ariadne.auth.rbac import Permission, check_permission
from ariadne.experiment.dataset import DatasetItem, parse_jsonl, to_jsonl
from ariadne.storage.postgres.repositories import (
    DatasetNotFoundError,
    DatasetRepository,
    DatasetVersionConflictError,
)

router = APIRouter(tags=["datasets"])

MAX_ITEMS_PER_REQUEST = 10_000


class ItemPayload(BaseModel):
    item_id: str = Field(min_length=1, max_length=200)
    input: str
    expected: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


class CreateDatasetRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    items: list[ItemPayload] = Field(min_length=1, max_length=MAX_ITEMS_PER_REQUEST)
    version: int | None = Field(default=None, ge=1)
    description: str = ""


class DatasetSummary(BaseModel):
    name: str
    version: int
    content_hash: str
    item_count: int
    ref: str


class DatasetDetail(DatasetSummary):
    description: str
    items: list[ItemPayload]


class DatasetNameEntry(BaseModel):
    name: str
    latest_version: int


class VersionEntry(BaseModel):
    version: int
    content_hash: str
    item_count: int


def _to_domain_items(payloads: list[ItemPayload]) -> list[DatasetItem]:
    return [
        DatasetItem(
            item_id=p.item_id,
            input=p.input,
            expected=p.expected,
            metadata=p.metadata,
        )
        for p in payloads
    ]


@router.post(
    "/datasets",
    status_code=status.HTTP_201_CREATED,
    response_model=DatasetSummary,
    summary="创建数据集版本",
)
async def create_dataset(
    body: CreateDatasetRequest, ctx: TenantCtx, pg: TenantPg
) -> DatasetSummary:
    check_permission(ctx.role, Permission.WRITE)
    async with pg.session() as session:
        repo = DatasetRepository(session)
        try:
            dataset = await repo.create(
                project_id=ctx.project_id,
                name=body.name,
                items=_to_domain_items(body.items),
                version=body.version,
                description=body.description,
            )
        except DatasetVersionConflictError as exc:
            raise ConflictError(str(exc)) from exc
        except ValueError as exc:
            # 重复 item_id 等领域校验失败
            raise BadRequestError(str(exc)) from exc

    return DatasetSummary(
        name=dataset.name,
        version=dataset.version,
        content_hash=dataset.content_hash,
        item_count=len(dataset),
        ref=dataset.ref,
    )


@router.get("/datasets", response_model=list[DatasetNameEntry], summary="数据集列表")
async def list_datasets(ctx: TenantCtx, pg: TenantPg) -> list[DatasetNameEntry]:
    check_permission(ctx.role, Permission.READ)
    async with pg.session() as session:
        names = await DatasetRepository(session).list_names(project_id=ctx.project_id)
    return [DatasetNameEntry(name=n, latest_version=v) for n, v in names]


@router.get(
    "/datasets/{name}/versions",
    response_model=list[VersionEntry],
    summary="某数据集的全部版本",
)
async def list_versions(
    name: str, ctx: TenantCtx, pg: TenantPg
) -> list[VersionEntry]:
    check_permission(ctx.role, Permission.READ)
    async with pg.session() as session:
        rows = await DatasetRepository(session).list_versions(
            project_id=ctx.project_id, name=name
        )
    if not rows:
        raise NotFoundError(f"数据集不存在: {name}", name=name)
    return [
        VersionEntry(version=v, content_hash=h, item_count=c) for v, h, c in rows
    ]


@router.get(
    "/datasets/{name}", response_model=DatasetDetail, summary="数据集详情"
)
async def get_dataset(
    name: str,
    ctx: TenantCtx,
    pg: TenantPg,
    version: Annotated[int | None, Query(ge=1)] = None,
) -> DatasetDetail:
    check_permission(ctx.role, Permission.READ)
    async with pg.session() as session:
        try:
            dataset = await DatasetRepository(session).get(
                project_id=ctx.project_id, name=name, version=version
            )
        except DatasetNotFoundError as exc:
            raise NotFoundError(str(exc), name=name, version=version) from exc

    return DatasetDetail(
        name=dataset.name,
        version=dataset.version,
        content_hash=dataset.content_hash,
        item_count=len(dataset),
        ref=dataset.ref,
        description=dataset.description,
        items=[
            ItemPayload(
                item_id=i.item_id,
                input=i.input,
                expected=i.expected,
                metadata=i.metadata,
            )
            for i in dataset.items
        ],
    )


@router.get(
    "/datasets/{name}/export",
    summary="导出为 JSONL",
    response_class=PlainTextResponse,
)
async def export_dataset(
    name: str,
    ctx: TenantCtx,
    pg: TenantPg,
    version: Annotated[int | None, Query(ge=1)] = None,
) -> PlainTextResponse:
    """导出 JSONL。往返转换后 content_hash 不变（有测试保证）。"""
    check_permission(ctx.role, Permission.READ)
    async with pg.session() as session:
        try:
            dataset = await DatasetRepository(session).get(
                project_id=ctx.project_id, name=name, version=version
            )
        except DatasetNotFoundError as exc:
            raise NotFoundError(str(exc), name=name) from exc

    filename = f"{dataset.name}-v{dataset.version}.jsonl"
    return PlainTextResponse(
        to_jsonl(dataset),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class ImportRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    jsonl: str = Field(min_length=1)
    description: str = ""


@router.post(
    "/datasets/import",
    status_code=status.HTTP_201_CREATED,
    response_model=DatasetSummary,
    summary="从 JSONL 导入",
)
async def import_dataset(
    body: ImportRequest, ctx: TenantCtx, pg: TenantPg
) -> DatasetSummary:
    """从 JSONL 导入。

    解析**不容错**：静默跳过坏行会导致"两次实验用的其实不是同一数据集"。
    """
    check_permission(ctx.role, Permission.WRITE)
    try:
        items = parse_jsonl(body.jsonl.splitlines())
    except ValueError as exc:
        raise BadRequestError(f"JSONL 解析失败: {exc}") from exc

    async with pg.session() as session:
        try:
            dataset = await DatasetRepository(session).create(
                project_id=ctx.project_id,
                name=body.name,
                items=items,
                description=body.description,
            )
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc

    return DatasetSummary(
        name=dataset.name,
        version=dataset.version,
        content_hash=dataset.content_hash,
        item_count=len(dataset),
        ref=dataset.ref,
    )
