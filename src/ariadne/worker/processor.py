"""加工管线：成本计算 → 脱敏 → payload 分级。

放在 Worker 侧而非 API 侧的理由：同步路径只做入队才能保证

SDK < 1ms 开销与 API 的高吞吐。加工是 CPU 密集的，必须异步化。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from ariadne.config import Settings
from ariadne.storage.objectstore import PayloadProcessor
from ariadne.telemetry.adapters import adapt_batch
from ariadne.telemetry.models import AriadneSpan, SpanKind
from ariadne.telemetry.pricing import PricingTable
from ariadne.telemetry.redaction import RedactionContext, redact
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ProcessResult:
    spans: tuple[AriadneSpan, ...]
    adapt_errors: tuple[str, ...]
    redaction_hits: int
    spilled_count: int

    @property
    def total_cost(self) -> Decimal:
        return sum((s.cost_usd for s in self.spans), Decimal("0"))


class SpanProcessor:
    def __init__(
        self,
        settings: Settings,
        payloads: PayloadProcessor | None = None,
        pricing: PricingTable | None = None,
    ) -> None:
        self._settings = settings
        self._pricing = pricing or PricingTable()
        self._payloads = payloads or PayloadProcessor(
            settings.payload,
            store_full_payload=settings.telemetry.store_full_payload,
        )

    def set_db_pricing(self, table: PricingTable) -> None:
        """注入 DB 计价表（model_pricing）。Collector 启动时调用。"""
        self._pricing = table

    def process_batch(
        self, format_name: str, records: list[dict[str, object]], project_id: UUID
    ) -> ProcessResult:
        spans, errors = adapt_batch(format_name, records, project_id)

        # 脱敏上下文按 trace 分组，保证同一实体在一条 trace 内映射一致
        contexts: dict[str, RedactionContext] = {}
        enriched: list[AriadneSpan] = []
        spilled = 0

        for span in spans:
            ctx = contexts.setdefault(span.trace_id, RedactionContext(span.trace_id))
            processed, did_spill = self._enrich(span, ctx)
            enriched.append(processed)
            spilled += int(did_spill)

        hits = sum(c.hit_count for c in contexts.values())
        if errors:
            logger.warning(
                "adapt errors in batch",
                extra={"format": format_name, "error_count": len(errors),
                       "samples": errors[:3]},
            )
        return ProcessResult(tuple(enriched), tuple(errors), hits, spilled)

    def _enrich(self, span: AriadneSpan, ctx: RedactionContext) -> tuple[AriadneSpan, bool]:
        updates: dict[str, object] = {}

        # 1. 成本：只对 LLM span 计算，且一律服务端算（忽略客户端上报）
        if span.kind is SpanKind.LLM and span.usage.total_tokens > 0:
            model = span.model_response or span.model_request
            updates["cost_usd"] = self._pricing.compute(
                span.provider, model, span.usage, span.started_at
            )

        # 2. 脱敏：先脱敏再分级，避免明文 PII 落到对象存储
        input_text = span.input_preview
        output_text = span.output_preview
        if self._settings.telemetry.redaction_enabled:
            input_text = redact(input_text, ctx)
            output_text = redact(output_text, ctx)

        # 3. payload 分级
        did_spill = False
        if input_text:
            stored = self._payloads.process(
                input_text, project_id=str(span.project_id), span_id=span.span_id, slot="in"
            )
            updates["input_preview"] = stored.preview
            updates["input_ref"] = stored.ref
            did_spill = did_spill or bool(stored.ref)
        if output_text:
            stored = self._payloads.process(
                output_text, project_id=str(span.project_id), span_id=span.span_id, slot="out"
            )
            updates["output_preview"] = stored.preview
            updates["output_ref"] = stored.ref
            did_spill = did_spill or bool(stored.ref)

        if not updates:
            return span, False
        return span.model_copy(update=updates), did_spill
