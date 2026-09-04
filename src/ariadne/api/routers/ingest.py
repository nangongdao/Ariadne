"""遥测接入路由。

设计要点：同步路径只做**校验 + 入队**，不做任何加工。这是 SDK < 1ms
与 API 高吞吐的前提，也让 API 无状态可水平扩展。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field

from ariadne.api.deps import AppSettings, Queue, TenantCtx
from ariadne.api.errors import BatchTooLargeError, UnknownFormatError
from ariadne.auth.rbac import Permission, check_permission
from ariadne.telemetry.adapters import available_formats
from ariadne.telemetry.adapters.otlp import flatten_otlp_attributes
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)
router = APIRouter(tags=["ingest"])


class IngestAccepted(BaseModel):
    accepted: int
    queued_message_id: str


class NativeBatch(BaseModel):
    spans: list[dict[str, Any]] = Field(min_length=1)


def _flatten_otlp(body: dict[str, Any]) -> list[dict[str, Any]]:
    """把 OTLP 的 resourceSpans/scopeSpans/spans 三层结构展平为单 span 列表。

    resource 属性（service.name 等）合并到每个 span 的 _resource 字段，
    因为它们对排障有用但 OTLP 把它们放在了外层。
    """
    records: list[dict[str, Any]] = []
    for resource_span in body.get("resourceSpans", []):
        resource_attrs = flatten_otlp_attributes(
            (resource_span.get("resource") or {}).get("attributes")
        )
        for scope_span in resource_span.get("scopeSpans", []):
            scope = scope_span.get("scope") or {}
            for span in scope_span.get("spans", []):
                record = dict(span)
                record["_resource"] = resource_attrs
                if scope.get("name"):
                    record["_scope"] = str(scope["name"])
                records.append(record)
    return records


async def _enqueue(
    queue: Queue, fmt: str, project_id: Any, records: list[dict[str, Any]], limit: int
) -> IngestAccepted:
    if len(records) > limit:
        raise BatchTooLargeError(
            f"单批最多 {limit} 条 span，收到 {len(records)} 条", limit=limit
        )
    message_id = await queue.publish(
        {
            "format": fmt,
            "project_id": str(project_id),
            "received_at": datetime.now(UTC).isoformat(),
            "payload": records,
        }
    )
    logger.debug("batch queued", extra={"format": fmt, "count": len(records)})
    return IngestAccepted(accepted=len(records), queued_message_id=message_id)


@router.post(
    "/ingest/spans",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestAccepted,
    summary="上报 span（Ariadne 原生格式）",
)
async def ingest_native(
    batch: NativeBatch, ctx: TenantCtx, queue: Queue, settings: AppSettings
) -> IngestAccepted:
    check_permission(ctx.role, Permission.WRITE)
    return await _enqueue(
        queue, "native", ctx.project_id, batch.spans, settings.api.max_batch_spans
    )


@router.post(
    "/traces",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestAccepted,
    summary="OTLP/HTTP 接入（JSON / protobuf 编码）",
)
async def ingest_otlp(
    request: Request, ctx: TenantCtx, queue: Queue, settings: AppSettings
) -> IngestAccepted:
    """标准 OTel exporter 可直接指向此端点。

    支持 OTLP/HTTP JSON 与 protobuf 两种编码。
    protobuf 路径用 opentelemetry-proto 解码为与 JSON 相同的字典结构。
    """
    check_permission(ctx.role, Permission.WRITE)
    content_type = request.headers.get("content-type", "")

    if "protobuf" in content_type:
        body = await request.body()
        if not body:
            return IngestAccepted(accepted=0, queued_message_id="")
        try:
            from ariadne.telemetry.otlp_protobuf import decode_otlp_protobuf

            data = decode_otlp_protobuf(body)
        except Exception as exc:
            logger.warning("otlp protobuf decode failed", extra={"error": str(exc)})
            raise UnknownFormatError(
                f"OTLP protobuf 解码失败: {exc}"
            ) from exc
        records = _flatten_otlp(data)
    else:
        body = await request.json()
        records = _flatten_otlp(body)

    if not records:
        return IngestAccepted(accepted=0, queued_message_id="")
    return await _enqueue(
        queue, "otlp", ctx.project_id, records, settings.api.max_batch_spans
    )


@router.post(
    "/ingest/openinference",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=IngestAccepted,
    summary="OpenInference 格式接入",
)
async def ingest_openinference(
    batch: NativeBatch, ctx: TenantCtx, queue: Queue, settings: AppSettings
) -> IngestAccepted:
    check_permission(ctx.role, Permission.WRITE)
    return await _enqueue(
        queue, "openinference", ctx.project_id, batch.spans, settings.api.max_batch_spans
    )


@router.get("/ingest/formats", summary="列出已注册的遥测格式")
async def list_formats() -> dict[str, list[str]]:
    return {"formats": available_formats()}
