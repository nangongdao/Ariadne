"""PII 脱敏。

关键约束：同一实体在一次 trace 内必须映射到同一占位符。否则脱敏后的输出
丧失可读性，无法用于排障 —— 用户会看到 [PERSON_1] 和 [PERSON_7] 其实是同一人。
"""

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final


class PiiKind(StrEnum):
    EMAIL = "email"
    PHONE_CN = "phone_cn"
    ID_CARD_CN = "id_card_cn"
    BANK_CARD = "bank_card"
    API_KEY = "api_key"
    IPV4 = "ipv4"
    CUSTOM = "custom"


# 顺序即优先级：先匹配的先脱敏，避免 api_key 里的数字被当成手机号。
# 所有正则都避免嵌套量词，防止 ReDoS。
_PATTERNS: Final[tuple[tuple[PiiKind, re.Pattern[str]], ...]] = (
    (
        # 需覆盖多段格式（sk-proj-xxx、ak_live_xxx）。前置 lookahead 要求
        # 后续至少 12 个字符，避免把 "ak-test" 这类短串误判为凭证。
        PiiKind.API_KEY,
        re.compile(
            r"\b(?:sk|ak|pk|ghp|gho|xox[baprs])(?=[-_][A-Za-z0-9_-]{12,})"
            r"(?:[-_][A-Za-z0-9]{3,}){1,5}\b"
        ),
    ),
    (PiiKind.EMAIL, re.compile(r"\b[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,4}\b")),
    (PiiKind.ID_CARD_CN, re.compile(r"\b\d{17}[\dXx]\b")),
    (PiiKind.BANK_CARD, re.compile(r"\b\d{16,19}\b")),
    (PiiKind.PHONE_CN, re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    (PiiKind.IPV4, re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
)


@dataclass
class RedactionContext:
    """一次 trace 内共享，保证实体到占位符的映射稳定。"""

    trace_id: str
    _mapping: dict[tuple[PiiKind, str], str] = field(default_factory=dict)
    _counters: dict[PiiKind, int] = field(default_factory=dict)

    def placeholder_for(self, kind: PiiKind, value: str) -> str:
        key = (kind, value)
        existing = self._mapping.get(key)
        if existing is not None:
            return existing
        self._counters[kind] = self._counters.get(kind, 0) + 1
        token = f"[{kind.value.upper()}_{self._counters[kind]}]"
        self._mapping[key] = token
        return token

    @property
    def hit_count(self) -> int:
        return len(self._mapping)

    def hit_kinds(self) -> list[str]:
        return sorted({kind.value for kind, _ in self._mapping})


def _mask_email(value: str) -> str:
    """邮箱掩码保留结构，仍可辨认域名，便于排障。"""
    local, _, domain = value.partition("@")
    keep = local[:1] if local else ""
    return f"{keep}{'*' * max(len(local) - 1, 3)}@{domain}"


def _mask_tail(value: str, keep: int = 4) -> str:
    return f"{'*' * max(len(value) - keep, 0)}{value[-keep:]}"


def redact(text: str, ctx: RedactionContext) -> str:
    """按类型选择策略脱敏。

    结构性 PII（邮箱/手机/卡号）掩码保留结构；凭证类完全替换；
    其余用 trace 内稳定占位符。
    """
    if not text:
        return text

    result = text
    for kind, pattern in _PATTERNS:

        def _sub(m: re.Match[str], _kind: PiiKind = kind) -> str:
            raw = m.group(0)
            # 记录以维持 trace 内映射与命中统计
            ctx.placeholder_for(_kind, raw)
            if _kind is PiiKind.API_KEY:
                return "[REDACTED:api_key]"
            if _kind is PiiKind.EMAIL:
                return _mask_email(raw)
            if _kind in (PiiKind.PHONE_CN, PiiKind.BANK_CARD, PiiKind.ID_CARD_CN):
                return _mask_tail(raw)
            return ctx.placeholder_for(_kind, raw)

        result = pattern.sub(_sub, result)
    return result


def detect_pii(text: str) -> list[str]:
    """只检测不改写，供 Harness 规则的 detect_pii() 使用（M4）。"""
    return sorted({kind.value for kind, pattern in _PATTERNS if pattern.search(text)})
