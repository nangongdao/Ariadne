"""自有 JSON 格式适配器。

这是 Ariadne SDK 的上报格式：字段名与 AriadneSpan 一致，转换基本是直通。
存在的意义是让不想引入 OTel 依赖的用户也能接入。
"""

from typing import Any
from uuid import UUID

from ariadne.telemetry.adapters import register_adapter
from ariadne.telemetry.adapters.base import BaseAdapter
from ariadne.telemetry.models import AriadneSpan, TokenUsage


@register_adapter("native")
class NativeAdapter(BaseAdapter):
    def adapt(self, raw: dict[str, object], project_id: UUID) -> AriadneSpan:
        data: dict[str, Any] = dict(raw)
        usage_raw: dict[str, Any] = data.get("usage") or {}

        return AriadneSpan(
            project_id=project_id,
            trace_id=self.trace_id(data, "trace_id"),
            span_id=self.span_id(data, "span_id"),
            parent_span_id=self.parent_id(data, "parent_span_id"),
            name=self.s(data, "name", default="unnamed"),
            kind=self.normalize_kind(self.s(data, "kind", default="internal")),
            operation=self.s(data, "operation"),
            provider=self.s(data, "provider"),
            model_request=self.s(data, "model_request", "model"),
            model_response=self.s(data, "model_response"),
            started_at=self.parse_time(data.get("started_at")),
            duration_ms=self.i(data, "duration_ms"),
            status=self.normalize_status(
                self.s(data, "status", default="ok"),
                has_error=bool(self.s(data, "error_type")),
            ),
            error_type=self.s(data, "error_type"),
            usage=TokenUsage(
                input_tokens=self.i(usage_raw, "input_tokens"),
                output_tokens=self.i(usage_raw, "output_tokens"),
                cache_read_tokens=self.i(usage_raw, "cache_read_tokens"),
                cache_write_tokens=self.i(usage_raw, "cache_write_tokens"),
                reasoning_tokens=self.i(usage_raw, "reasoning_tokens"),
            ),
            # cost_usd 一律由服务端算，忽略客户端上报值以防伪造
            loop_id=self.s(data, "loop_id"),
            iteration=self.i(data, "iteration"),
            input_preview=self.s(data, "input_preview"),
            output_preview=self.s(data, "output_preview"),
            attributes=self.flatten_attributes(data.get("attributes") or {}),
            tags=[str(t) for t in (data.get("tags") or [])][:32],
        )
