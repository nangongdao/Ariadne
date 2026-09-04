"""OTLP/HTTP JSON 适配器（OTel GenAI semconv）。

映射表集中在 _GENAI_KEYS：上游属性改名只需改这里。M1 只支持 JSON 编码，
protobuf 留到 M2（需要 opentelemetry-proto 依赖，且 JSON 已够验证链路）。
"""

from typing import Any, Final
from uuid import UUID

from ariadne.telemetry.adapters import register_adapter
from ariadne.telemetry.adapters.base import NANOS_PER_MS, BaseAdapter
from ariadne.telemetry.models import AriadneSpan, TokenUsage

# 每项按优先级列出候选键名，兼容不同 semconv 版本与厂商变体
_GENAI_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "operation": ("gen_ai.operation.name",),
    "provider": ("gen_ai.provider.name", "gen_ai.system"),
    "model_request": ("gen_ai.request.model",),
    "model_response": ("gen_ai.response.model",),
    "input_tokens": ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens"),
    "output_tokens": ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens"),
    "cache_read_tokens": ("gen_ai.usage.cache_read_input_tokens",),
    "cache_write_tokens": ("gen_ai.usage.cache_creation_input_tokens",),
    "reasoning_tokens": ("gen_ai.usage.reasoning_tokens",),
}

_OTLP_KIND_HINTS: Final[dict[str, str]] = {
    "chat": "llm",
    "text_completion": "llm",
    "embeddings": "llm",
    "execute_tool": "tool",
    "invoke_agent": "internal",
}

# _GENAI_KEYS 是按这个 semconv 版本的属性名写的。settings.telemetry
# .genai_semconv_version 与它不一致时说明有人升了版本却没更映射表 ——
# docs/06 §1 要求版本锁定是"一次带迁移的显式操作"，静默漂移正是该纪律要防的。
MAPPED_SEMCONV_VERSION: Final = "1.37.0"


def check_semconv_version(configured: str) -> str | None:
    """校验配置的 semconv 版本与映射表是否匹配。

    返回不匹配的说明，匹配则返回 None。不抛异常 —— 版本漂移是要提醒运维的
    风险，不是应当阻断采集管道的错误（丢遥测数据比属性名过时更糟）。
    """
    if configured == MAPPED_SEMCONV_VERSION:
        return None
    return (
        f"配置的 GenAI semconv 版本 {configured!r} 与适配层映射表所依据的 "
        f"{MAPPED_SEMCONV_VERSION!r} 不一致：升级 semconv 需同时复核 "
        f"_GENAI_KEYS 的属性名，否则新版改名的属性会静默读不到"
    )


def _unwrap_any_value(value: Any) -> str:
    """OTLP 的 AnyValue 是带类型标签的 wrapper，取出标量值。"""
    if not isinstance(value, dict):
        return "" if value is None else str(value)
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return str(value[key])
    if "arrayValue" in value:
        items = value["arrayValue"].get("values", [])
        return ",".join(_unwrap_any_value(v) for v in items)
    return str(value)


def flatten_otlp_attributes(attrs: list[dict[str, Any]] | None) -> dict[str, str]:
    """OTLP 属性是 [{key, value:{stringValue:...}}] 数组，压成平字典。"""
    flat: dict[str, str] = {}
    for item in attrs or []:
        key = item.get("key")
        if key:
            flat[str(key)] = _unwrap_any_value(item.get("value"))
    return flat


@register_adapter("otlp")
class OtlpAdapter(BaseAdapter):
    def adapt(self, raw: dict[str, object], project_id: UUID) -> AriadneSpan:
        data: dict[str, Any] = dict(raw)
        # 调用方（ingest 路由）已把 resourceSpans 展平为单 span dict，
        # 并把 resource 属性合并进 _resource
        attrs = flatten_otlp_attributes(data.get("attributes"))
        resource = data.get("_resource") or {}

        def pick(field: str, default: str = "") -> str:
            return self.s(attrs, *_GENAI_KEYS[field], default=default)

        def pick_int(field: str) -> int:
            return self.i(attrs, *_GENAI_KEYS[field])

        start_ns = self.i(data, "startTimeUnixNano")
        end_ns = self.i(data, "endTimeUnixNano")
        duration_ms = max((end_ns - start_ns) // NANOS_PER_MS, 0) if end_ns else 0

        operation = pick("operation")
        status_raw = data.get("status") or {}
        status_code = self.s(status_raw, "code", default="")

        span_name = self.s(data, "name", default="unnamed")
        kind_hint = _OTLP_KIND_HINTS.get(operation, "")
        if not kind_hint:
            kind_hint = "llm" if operation or pick("model_request") else "internal"

        return AriadneSpan(
            project_id=project_id,
            trace_id=self.trace_id(data, "traceId", "trace_id"),
            span_id=self.span_id(data, "spanId", "span_id"),
            parent_span_id=self.parent_id(data, "parentSpanId", "parent_span_id"),
            name=span_name,
            kind=self.normalize_kind(kind_hint),
            operation=operation,
            provider=pick("provider"),
            model_request=pick("model_request"),
            model_response=pick("model_response"),
            started_at=self.parse_time(start_ns),
            duration_ms=duration_ms,
            status=self.normalize_status(status_code),
            error_type=self.s(status_raw, "message") if status_code else "",
            usage=TokenUsage(
                input_tokens=pick_int("input_tokens"),
                output_tokens=pick_int("output_tokens"),
                cache_read_tokens=pick_int("cache_read_tokens"),
                cache_write_tokens=pick_int("cache_write_tokens"),
                reasoning_tokens=pick_int("reasoning_tokens"),
            ),
            loop_id=self.s(attrs, "ariadne.loop.id"),
            iteration=self.i(attrs, "ariadne.loop.iteration"),
            failure_fp=self.s(attrs, "ariadne.loop.failure_fp"),
            input_preview=self.s(attrs, "gen_ai.prompt", "gen_ai.input.messages"),
            output_preview=self.s(attrs, "gen_ai.completion", "gen_ai.output.messages"),
            attributes={
                **{k: v for k, v in resource.items() if k.startswith("service.")},
                **{k: v for k, v in attrs.items() if not k.startswith("gen_ai.usage.")},
            },
            tags=[],
        )
