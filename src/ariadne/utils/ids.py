"""Trace / Span ID 生成与归一化。

遵循 W3C Trace Context：trace_id 32 位 hex，span_id 16 位 hex。
外部来源的 ID 可能是 base64（OTLP protobuf JSON 映射）或带连字符，
统一在 normalize_* 里处理。
"""

import base64
import binascii
import re
import secrets
from typing import Final

_HEX32_RE: Final = re.compile(r"^[0-9a-f]{32}$")
_HEX16_RE: Final = re.compile(r"^[0-9a-f]{16}$")
_NON_HEX_RE: Final = re.compile(r"[^0-9a-fA-F]")

TRACE_ID_BYTES: Final = 16
SPAN_ID_BYTES: Final = 8


def new_trace_id() -> str:
    return secrets.token_hex(TRACE_ID_BYTES)


def new_span_id() -> str:
    return secrets.token_hex(SPAN_ID_BYTES)


def _from_base64(value: str, expect_bytes: int) -> str | None:
    """OTLP 的 JSON 编码会把 ID 字节序列表示为 base64。"""
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(decoded) != expect_bytes:
        return None
    return decoded.hex()


def _normalize(value: str, expect_bytes: int, pattern: re.Pattern[str]) -> str:
    if not value:
        return ""

    lowered = value.strip().lower()
    if pattern.match(lowered):
        return lowered

    # 去掉连字符等分隔符后再试（UUID 风格的 trace_id）
    stripped = _NON_HEX_RE.sub("", value).lower()
    if pattern.match(stripped):
        return stripped

    from_b64 = _from_base64(value.strip(), expect_bytes)
    if from_b64:
        return from_b64

    # 左补零仅用于"SDK 去掉了前导零"这一种情况，因此要求剩余长度已接近完整。
    # 否则 "!!!invalid!!!" 会被剥成 "ad" 再补成合法 ID，凭空造出不存在的父节点。
    expected_len = expect_bytes * 2
    if stripped and expected_len // 2 <= len(stripped) < expected_len:
        return stripped.rjust(expected_len, "0")
    return ""


def normalize_trace_id(value: str) -> str:
    """归一化 trace_id；无法识别时返回空字符串，由调用方决定丢弃或补生成。"""
    return _normalize(value, TRACE_ID_BYTES, _HEX32_RE)


def normalize_span_id(value: str) -> str:
    return _normalize(value, SPAN_ID_BYTES, _HEX16_RE)


def is_valid_trace_id(value: str) -> bool:
    return bool(_HEX32_RE.match(value))


def is_valid_span_id(value: str) -> bool:
    return bool(_HEX16_RE.match(value))
