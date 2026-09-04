"""OTLP/HTTP protobuf 解码器。

将 OTLP protobuf 二进制体解码为与 JSON 路径相同的字典结构，
使下游 `_flatten_otlp` + `OtlpAdapter` 无需改动。

关键差异：protobuf 二进制的 trace_id/span_id 是 bytes，
`google.protobuf.json_format.MessageToDict` 默认编码为 base64，
而 OTLP/HTTP JSON 发送的是 hex 字符串。这里用自定义转换器保持一致。

M6 验收项 #11：同一 trace 两种编码上报，落库结果一致。
"""

from __future__ import annotations

import logging
from typing import Any

from google.protobuf.json_format import MessageToDict

logger = logging.getLogger(__name__)

# 需要转为 hex 而非 base64 的 bytes 字段
_HEX_FIELDS: frozenset[str] = frozenset({"traceId", "spanId", "parentSpanId"})


def _convert_value(value: Any, field_name: str) -> Any:
    """递归转换 protobuf dict，把 bytes 字段从 base64 转为 hex。

    MessageToDict 已把 bytes 编码为 base64 字符串，我们只需识别
    已知字段名并重编码。
    """
    if isinstance(value, str) and field_name in _HEX_FIELDS:
        import base64

        try:
            raw = base64.b64decode(value)
            return raw.hex()
        except Exception:
            return value
    return value


def _walk(obj: Any, field_name: str = "") -> Any:
    """递归遍历 MessageToDict 的输出，对 hex 字段做转换。"""
    if isinstance(obj, dict):
        return {k: _walk(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk(item, field_name) for item in obj]
    return _convert_value(obj, field_name)


def decode_otlp_protobuf(body: bytes) -> dict[str, Any]:
    """解码 OTLP protobuf 请求体为 JSON 兼容的字典。

    返回结构与 OTLP/HTTP JSON 的 `ExportTraceServiceRequest` 一致：
    ``{"resourceSpans": [...]}``

    Raises:
        Exception: 如果 protobuf 解析失败。
    """
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    request = ExportTraceServiceRequest()
    request.ParseFromString(body)
    raw = MessageToDict(request)  # camelCase，与 JSON 路径一致
    return _walk(raw)  # type: ignore[no-any-return]


__all__ = ["decode_otlp_protobuf"]
