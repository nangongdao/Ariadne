"""OTLP protobuf 解码测试。

验收项 #11：同一 trace 两种编码上报，落库结果一致。
"""

from __future__ import annotations

from ariadne.telemetry.otlp_protobuf import decode_otlp_protobuf


def _make_protobuf_request() -> bytes:
    """构造一个包含单个 span 的 OTLP protobuf 请求。"""
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue

    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    rs.resource.attributes.add(
        key="service.name", value=AnyValue(string_value="test-svc")
    )
    ss = rs.scope_spans.add()
    span = ss.spans.add()
    span.trace_id = b"\x61" * 16  # 16 bytes = 32 hex chars
    span.span_id = b"\x62" * 8  # 8 bytes = 16 hex chars
    span.name = "chat"
    span.start_time_unix_nano = 1000000000
    span.end_time_unix_nano = 2000000000
    span.attributes.add(
        key="gen_ai.operation.name", value=AnyValue(string_value="chat")
    )
    return req.SerializeToString()


class TestDecodeOtlpProtobuf:
    def test_decodes_to_camelcase_structure(self) -> None:
        """解码后结构与 OTLP JSON 一致（camelCase）。"""
        body = _make_protobuf_request()
        result = decode_otlp_protobuf(body)

        assert "resourceSpans" in result
        rs_list = result["resourceSpans"]
        assert len(rs_list) == 1

        rs = rs_list[0]
        assert "resource" in rs
        attrs = rs["resource"]["attributes"]
        assert attrs[0]["key"] == "service.name"
        assert attrs[0]["value"]["stringValue"] == "test-svc"

        ss_list = rs["scopeSpans"]
        assert len(ss_list) == 1
        spans = ss_list[0]["spans"]
        assert len(spans) == 1

    def test_trace_id_converted_to_hex(self) -> None:
        """trace_id 从 base64 bytes 转为 hex 字符串（与 JSON 路径一致）。"""
        body = _make_protobuf_request()
        result = decode_otlp_protobuf(body)
        span = result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        # 16 bytes of 0x61 = 32 hex chars "6161..."
        assert span["traceId"] == "61" * 16

    def test_span_id_converted_to_hex(self) -> None:
        body = _make_protobuf_request()
        result = decode_otlp_protobuf(body)
        span = result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["spanId"] == "62" * 8

    def test_name_preserved(self) -> None:
        body = _make_protobuf_request()
        result = decode_otlp_protobuf(body)
        span = result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["name"] == "chat"

    def test_timestamps_preserved_as_string(self) -> None:
        body = _make_protobuf_request()
        result = decode_otlp_protobuf(body)
        span = result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["startTimeUnixNano"] == "1000000000"
        assert span["endTimeUnixNano"] == "2000000000"

    def test_attributes_preserved(self) -> None:
        body = _make_protobuf_request()
        result = decode_otlp_protobuf(body)
        span = result["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = span["attributes"]
        assert attrs[0]["key"] == "gen_ai.operation.name"
        assert attrs[0]["value"]["stringValue"] == "chat"

    def test_empty_body_raises(self) -> None:
        """空 protobuf 解码为空 ExportTraceServiceRequest（无 resourceSpans）。"""
        result = decode_otlp_protobuf(b"")
        assert result == {}

    def test_invalid_protobuf_raises(self) -> None:
        """无效 protobuf 应抛异常（不是静默返回空）。"""
        import pytest
        from google.protobuf.message import DecodeError

        with pytest.raises(DecodeError):
            decode_otlp_protobuf(b"not a valid protobuf")
