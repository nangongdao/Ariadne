"""OpenInference（Arize 系）适配器。

支持它的理由：这套约定在 LlamaIndex / Arize Phoenix 生态里已有相当装机量。
让已用 OpenInference 埋点的用户零改造接入，适配成本远低于获客成本。
"""

from typing import Any, Final
from uuid import UUID

from ariadne.telemetry.adapters import register_adapter
from ariadne.telemetry.adapters.base import BaseAdapter
from ariadne.telemetry.models import AriadneSpan, TokenUsage

_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "kind": ("openinference.span.kind", "span.kind"),
    "provider": ("llm.provider", "llm.system"),
    "model": ("llm.model_name", "llm.model"),
    "input_tokens": ("llm.token_count.prompt",),
    "output_tokens": ("llm.token_count.completion",),
    "cache_read_tokens": ("llm.token_count.prompt_details.cache_read",),
    "cache_write_tokens": ("llm.token_count.prompt_details.cache_write",),
    "reasoning_tokens": ("llm.token_count.completion_details.reasoning",),
    "input": ("input.value", "llm.input_messages"),
    "output": ("output.value", "llm.output_messages"),
}


@register_adapter("openinference")
class OpenInferenceAdapter(BaseAdapter):
    def adapt(self, raw: dict[str, object], project_id: UUID) -> AriadneSpan:
        data: dict[str, Any] = dict(raw)
        # OpenInference 的属性可能是平字典，也可能嵌在 attributes 下
        attrs = self.flatten_attributes(data.get("attributes") or data)

        def pick(field: str, default: str = "") -> str:
            return self.s(attrs, *_KEYS[field], default=default)

        model = pick("model")
        return AriadneSpan(
            project_id=project_id,
            trace_id=self.trace_id(data, "context.trace_id", "trace_id"),
            span_id=self.span_id(data, "context.span_id", "span_id"),
            parent_span_id=self.parent_id(data, "parent_id", "parent_span_id"),
            name=self.s(data, "name", default="unnamed"),
            kind=self.normalize_kind(pick("kind", "internal")),
            operation=pick("kind").lower(),
            provider=pick("provider"),
            model_request=model,
            model_response=self.s(attrs, "llm.response.model", default=model),
            started_at=self.parse_time(data.get("start_time") or data.get("started_at")),
            duration_ms=self.i(data, "duration_ms", "latency_ms"),
            status=self.normalize_status(
                self.s(data, "status_code", "status", default="ok")
            ),
            error_type=self.s(data, "status_message", "error_type"),
            usage=TokenUsage(
                input_tokens=self.i(attrs, *_KEYS["input_tokens"]),
                output_tokens=self.i(attrs, *_KEYS["output_tokens"]),
                cache_read_tokens=self.i(attrs, *_KEYS["cache_read_tokens"]),
                cache_write_tokens=self.i(attrs, *_KEYS["cache_write_tokens"]),
                reasoning_tokens=self.i(attrs, *_KEYS["reasoning_tokens"]),
            ),
            input_preview=pick("input"),
            output_preview=pick("output"),
            attributes={
                k: v for k, v in attrs.items() if not k.startswith("llm.token_count.")
            },
            tags=[],
        )
