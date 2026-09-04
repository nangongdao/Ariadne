"""加工管线测试：成本 → 脱敏 → payload 分级的组合行为。"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest

from ariadne.config import PayloadSettings, Settings, TelemetrySettings
from ariadne.storage.objectstore import ObjectStore, PayloadProcessor
from ariadne.worker.processor import SpanProcessor

if TYPE_CHECKING:
    pass

PID = UUID("00000000-0000-0000-0000-000000000001")


class MemoryStore(ObjectStore):
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> str:
        self.objects[key] = data
        return f"mem://{key}"

    def get(self, ref: str) -> bytes | None:
        return self.objects.get(ref.removeprefix("mem://"))

    def delete(self, ref: str) -> None:
        self.objects.pop(ref.removeprefix("mem://"), None)

    def delete_prefix(self, prefix: str) -> int:
        keys = [k for k in self.objects if k.startswith(prefix)]
        for k in keys:
            self.objects.pop(k, None)
        return len(keys)


def make_span(**overrides: Any) -> dict[str, Any]:
    base = {
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
        "name": "chat",
        "kind": "llm",
        "provider": "openai",
        "model": "gpt-4o",
        "started_at": "2026-08-25T10:00:00Z",
        "duration_ms": 100,
        "usage": {"input_tokens": 1_000_000, "output_tokens": 0},
    }
    return {**base, **overrides}


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def processor(store: MemoryStore) -> SpanProcessor:
    settings = Settings(
        payload=PayloadSettings(
            inline_max_bytes=100, compress_max_bytes=200, preview_chars=32
        ),
        telemetry=TelemetrySettings(redaction_enabled=True),
    )
    return SpanProcessor(settings, PayloadProcessor(settings.payload, store))


class TestCost:
    def test_cost_computed_server_side(self, processor: SpanProcessor) -> None:
        result = processor.process_batch("native", [make_span()], PID)
        assert result.spans[0].cost_usd == Decimal("2.50000000")

    def test_response_model_used_for_pricing(self, processor: SpanProcessor) -> None:
        """按实际响应版本计价，而非请求时的别名。"""
        result = processor.process_batch(
            "native", [make_span(model="gpt-4o", model_response="gpt-4o-mini")], PID
        )
        assert result.spans[0].cost_usd == Decimal("0.15000000")

    def test_non_llm_span_has_no_cost(self, processor: SpanProcessor) -> None:
        result = processor.process_batch(
            "native", [make_span(kind="tool", usage={"input_tokens": 1_000_000})], PID
        )
        assert result.spans[0].cost_usd == Decimal("0")


class TestRedaction:
    def test_pii_removed_before_storage(self, processor: SpanProcessor) -> None:
        result = processor.process_batch(
            "native", [make_span(input_preview="联系 alice@corp.com")], PID
        )
        assert "alice@corp.com" not in result.spans[0].input_preview
        assert result.redaction_hits == 1

    def test_consistent_within_trace_across_spans(
        self, processor: SpanProcessor
    ) -> None:
        """同一 trace 的两条 span 里的同一实体必须脱敏成一样的结果。"""
        result = processor.process_batch("native", [
            make_span(span_id="b" * 16, input_preview="alice@corp.com"),
            make_span(span_id="c" * 16, input_preview="alice@corp.com"),
        ], PID)
        assert result.spans[0].input_preview == result.spans[1].input_preview
        # 同一实体只计一次
        assert result.redaction_hits == 1

    def test_redaction_can_be_disabled(self, store: MemoryStore) -> None:
        settings = Settings(telemetry=TelemetrySettings(redaction_enabled=False))
        proc = SpanProcessor(settings, PayloadProcessor(settings.payload, store))
        result = proc.process_batch(
            "native", [make_span(input_preview="alice@corp.com")], PID
        )
        assert "alice@corp.com" in result.spans[0].input_preview


class TestPayloadTiering:
    def test_small_payload_inlined(self, processor: SpanProcessor) -> None:
        result = processor.process_batch(
            "native", [make_span(input_preview="short")], PID
        )
        span = result.spans[0]
        assert span.input_preview == "short"
        assert span.input_ref == ""

    def test_medium_payload_compressed_inline(self, processor: SpanProcessor) -> None:
        """100-200 字节：压缩后仍内联，带 zstd: 标记。"""
        text = "x" * 150
        result = processor.process_batch(
            "native", [make_span(input_preview=text)], PID
        )
        span = result.spans[0]
        assert span.input_preview.startswith("zstd:")
        assert span.input_ref == ""

    def test_large_payload_spilled(
        self, processor: SpanProcessor, store: MemoryStore
    ) -> None:
        """超阈值：外溢对象存储，库里只留引用 + 预览。"""
        text = "y" * 500
        result = processor.process_batch(
            "native", [make_span(input_preview=text)], PID
        )
        span = result.spans[0]
        assert span.input_ref.startswith("mem://")
        assert len(span.input_preview) == 32  # preview_chars
        assert result.spilled_count == 1
        assert len(store.objects) == 1

    def test_spilled_payload_roundtrips(
        self, processor: SpanProcessor, store: MemoryStore
    ) -> None:
        """外溢的内容必须能完整读回。"""
        text = "z" * 500
        result = processor.process_batch(
            "native", [make_span(input_preview=text)], PID
        )
        span = result.spans[0]
        payloads = PayloadProcessor(
            PayloadSettings(inline_max_bytes=100, compress_max_bytes=200), store
        )
        assert payloads.read(span.input_preview, span.input_ref) == text

    def test_compressed_inline_roundtrips(
        self, processor: SpanProcessor, store: MemoryStore
    ) -> None:
        text = "w" * 150
        result = processor.process_batch(
            "native", [make_span(input_preview=text)], PID
        )
        span = result.spans[0]
        payloads = PayloadProcessor(
            PayloadSettings(inline_max_bytes=100, compress_max_bytes=200), store
        )
        assert payloads.read(span.input_preview, span.input_ref) == text

    def test_pii_redacted_before_spill(
        self, processor: SpanProcessor, store: MemoryStore
    ) -> None:
        """关键顺序：先脱敏再外溢，明文 PII 绝不能落到对象存储。"""
        text = "alice@corp.com " + "x" * 500
        processor.process_batch("native", [make_span(input_preview=text)], PID)
        blob = next(iter(store.objects.values()))
        import zstandard

        stored = zstandard.ZstdDecompressor().decompress(blob).decode()
        assert "alice@corp.com" not in stored


class TestRobustness:
    def test_adapt_errors_reported_not_raised(self, processor: SpanProcessor) -> None:
        result = processor.process_batch("native", [
            make_span(),
            make_span(duration_ms=-1),
        ], PID)
        assert len(result.spans) == 1
        assert len(result.adapt_errors) == 1

    def test_total_cost_aggregated(self, processor: SpanProcessor) -> None:
        result = processor.process_batch("native", [
            make_span(span_id="b" * 16),
            make_span(span_id="c" * 16),
        ], PID)
        assert result.total_cost == Decimal("5.00000000")
