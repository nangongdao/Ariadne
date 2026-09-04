"""适配层测试。

适配层的契约是：**尽最大努力归一化，绝不因单条脏数据让整批失败**。
这里刻意用畸形输入验证这一点。
"""

import base64
from uuid import UUID

import pytest

from ariadne.telemetry.adapters import AdapterFactory, adapt_batch, available_formats
from ariadne.telemetry.models import SpanKind, SpanStatus

PID = UUID("00000000-0000-0000-0000-000000000001")


def test_all_three_formats_registered() -> None:
    assert set(available_formats()) == {"native", "otlp", "openinference"}


def test_unknown_format_raises_explicitly() -> None:
    """未知格式必须显式报错，不能静默回退到 native。"""
    with pytest.raises(ValueError, match="未知的遥测格式"):
        AdapterFactory("nonexistent")


class TestNative:
    def test_basic_mapping(self) -> None:
        spans, errors = adapt_batch("native", [{
            "trace_id": "a" * 32, "span_id": "b" * 16, "name": "chat",
            "kind": "llm", "provider": "openai", "model": "gpt-4o",
            "started_at": "2026-08-25T10:00:00Z", "duration_ms": 1200,
            "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 200},
        }], PID)
        assert not errors
        span = spans[0]
        assert span.kind is SpanKind.LLM
        assert span.model_request == "gpt-4o"
        assert span.usage.total_tokens == 350

    def test_client_reported_cost_is_ignored(self) -> None:
        """成本一律服务端算，防止客户端伪造。"""
        spans, _ = adapt_batch("native", [{
            "trace_id": "a" * 32, "span_id": "b" * 16, "name": "x",
            "started_at": "2026-08-25T10:00:00Z", "cost_usd": "999.99",
        }], PID)
        assert spans[0].cost_usd == 0


class TestOtlp:
    def test_base64_ids_decoded(self) -> None:
        """OTLP JSON 编码把 ID 表示为 base64，必须解回 hex。"""
        spans, errors = adapt_batch("otlp", [{
            "traceId": base64.b64encode(bytes.fromhex("c" * 32)).decode(),
            "spanId": base64.b64encode(bytes.fromhex("d" * 16)).decode(),
            "name": "chat", "startTimeUnixNano": 1756113600000000000,
            "endTimeUnixNano": 1756113601500000000, "attributes": [],
        }], PID)
        assert not errors
        assert spans[0].trace_id == "c" * 32
        assert spans[0].span_id == "d" * 16
        assert spans[0].duration_ms == 1500

    def test_anyvalue_wrappers_unwrapped(self) -> None:
        spans, _ = adapt_batch("otlp", [{
            "traceId": "e" * 32, "spanId": "f" * 16, "name": "chat",
            "startTimeUnixNano": 1756113600000000000,
            "endTimeUnixNano": 1756113600500000000,
            "attributes": [
                {"key": "gen_ai.provider.name", "value": {"stringValue": "anthropic"}},
                {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "1500"}},
                {"key": "gen_ai.request.temperature", "value": {"doubleValue": 0.7}},
            ],
        }], PID)
        span = spans[0]
        assert span.provider == "anthropic"
        assert span.usage.input_tokens == 1500
        assert span.attributes["gen_ai.request.temperature"] == "0.7"

    def test_legacy_attribute_names_still_work(self) -> None:
        """semconv 未稳定，旧名（prompt_tokens / gen_ai.system）必须兼容。"""
        spans, _ = adapt_batch("otlp", [{
            "traceId": "a" * 32, "spanId": "b" * 16, "name": "chat",
            "startTimeUnixNano": 1756113600000000000,
            "attributes": [
                {"key": "gen_ai.system", "value": {"stringValue": "openai"}},
                {"key": "gen_ai.usage.prompt_tokens", "value": {"intValue": "80"}},
                {"key": "gen_ai.usage.completion_tokens", "value": {"intValue": "20"}},
            ],
        }], PID)
        assert spans[0].provider == "openai"
        assert spans[0].usage.input_tokens == 80
        assert spans[0].usage.output_tokens == 20

    def test_error_status_mapped(self) -> None:
        spans, _ = adapt_batch("otlp", [{
            "traceId": "a" * 32, "spanId": "b" * 16, "name": "chat",
            "startTimeUnixNano": 1756113600000000000, "attributes": [],
            "status": {"code": "STATUS_CODE_ERROR", "message": "RateLimitError"},
        }], PID)
        assert spans[0].status is SpanStatus.ERROR
        assert spans[0].error_type == "RateLimitError"


class TestOpenInference:
    def test_nested_span_kind(self) -> None:
        spans, errors = adapt_batch("openinference", [{
            "context": {"trace_id": "a" * 32, "span_id": "b" * 16},
            "name": "retrieve", "start_time": 1756113600.0, "duration_ms": 340,
            "attributes": {"openinference": {"span": {"kind": "RETRIEVER"}}},
        }], PID)
        assert not errors
        assert spans[0].kind is SpanKind.RAG

    def test_token_count_mapping(self) -> None:
        spans, _ = adapt_batch("openinference", [{
            "context": {"trace_id": "a" * 32, "span_id": "b" * 16},
            "name": "llm", "start_time": 1756113600.0,
            "attributes": {
                "openinference": {"span": {"kind": "LLM"}},
                "llm": {"model_name": "gpt-4o",
                        "token_count": {"prompt": 120, "completion": 40}},
            },
        }], PID)
        assert spans[0].model_request == "gpt-4o"
        assert spans[0].usage.input_tokens == 120


class TestRobustness:
    def test_bad_record_does_not_kill_batch(self) -> None:
        """一条脏数据不能让整批入库失败。"""
        spans, errors = adapt_batch("native", [
            {"trace_id": "a" * 32, "span_id": "b" * 16, "name": "good",
             "started_at": "2026-08-25T10:00:00Z"},
            # 负数 duration 违反 ge=0 约束。空 name 不算脏数据 ——
            # 适配层会容错为 "unnamed"，这是刻意的（丢 span 比留个没名字的更糟）。
            {"trace_id": "e" * 32, "span_id": "f" * 16, "name": "bad",
             "started_at": "2026-08-25T10:00:00Z", "duration_ms": -5},
            {"trace_id": "c" * 32, "span_id": "d" * 16, "name": "also-good",
             "started_at": "2026-08-25T10:00:00Z"},
        ], PID)
        assert len(spans) == 2
        assert len(errors) == 1
        assert "#1" in errors[0]

    def test_missing_ids_are_generated(self) -> None:
        """缺 ID 不丢 span：生成新 ID 总比丢数据好。"""
        spans, errors = adapt_batch("native", [
            {"name": "no-ids", "started_at": "2026-08-25T10:00:00Z"}
        ], PID)
        assert not errors
        assert len(spans[0].trace_id) == 32
        assert len(spans[0].span_id) == 16

    def test_unparseable_parent_becomes_root(self) -> None:
        """父 ID 无法识别时留空（表示根），不能瞎生成一个不存在的父。"""
        spans, _ = adapt_batch("native", [{
            "trace_id": "a" * 32, "span_id": "b" * 16, "name": "x",
            "parent_span_id": "!!!invalid!!!", "started_at": "2026-08-25T10:00:00Z",
        }], PID)
        assert spans[0].parent_span_id == ""

    @pytest.mark.parametrize("ts", [
        1787652000, 1787652000000, 1787652000000000000,
        "2026-08-25T10:00:00Z", "2026-08-25T10:00:00+00:00",
    ])
    def test_timestamp_units_all_accepted(self, ts: object) -> None:
        """秒/毫秒/纳秒/ISO 混用是现实，全部要能解析到同一时刻。"""
        spans, errors = adapt_batch("native", [
            {"trace_id": "a" * 32, "span_id": "b" * 16, "name": "x", "started_at": ts}
        ], PID)
        assert not errors
        assert spans[0].started_at.year == 2026

    def test_attribute_count_capped(self) -> None:
        """属性膨胀必须被截断，否则单条 span 能打爆存储。"""
        spans, _ = adapt_batch("native", [{
            "trace_id": "a" * 32, "span_id": "b" * 16, "name": "x",
            "started_at": "2026-08-25T10:00:00Z",
            "attributes": {f"k{i}": "v" for i in range(500)},
        }], PID)
        assert len(spans[0].attributes) <= 128

    def test_empty_batch(self) -> None:
        spans, errors = adapt_batch("native", [], PID)
        assert spans == [] and errors == []
