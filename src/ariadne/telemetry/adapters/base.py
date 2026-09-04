"""适配器基类与容错取值工具。

遥测数据来自外部进程，字段缺失、类型不符、时间格式混乱都是常态。
适配层的原则是**尽最大努力归一化，绝不因单条脏数据抛异常**——
丢一条 span 可以接受，让整批入库失败不可接受。
"""

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from ariadne.telemetry.models import AriadneSpan, SpanKind, SpanStatus
from ariadne.utils.ids import normalize_span_id, normalize_trace_id

NANOS_PER_MS: Final = 1_000_000
NANOS_PER_SEC: Final = 1_000_000_000
# 判定时间戳单位的阈值：2001-09-09 之后的秒级时间戳大于此值
_SEC_THRESHOLD: Final = 1_000_000_000
_MS_THRESHOLD: Final = 1_000_000_000_000
_NS_THRESHOLD: Final = 1_000_000_000_000_000

_KIND_ALIASES: Final[dict[str, SpanKind]] = {
    "llm": SpanKind.LLM,
    "chat": SpanKind.LLM,
    "completion": SpanKind.LLM,
    "embedding": SpanKind.LLM,
    "embeddings": SpanKind.LLM,
    "tool": SpanKind.TOOL,
    "function": SpanKind.TOOL,
    "retriever": SpanKind.RAG,
    "rag": SpanKind.RAG,
    "reranker": SpanKind.RAG,
    "code": SpanKind.CODE,
    "loop": SpanKind.LOOP,
    "harness": SpanKind.HARNESS,
    "eval": SpanKind.EVAL,
    "evaluator": SpanKind.EVAL,
    "agent": SpanKind.INTERNAL,
    "chain": SpanKind.INTERNAL,
    "internal": SpanKind.INTERNAL,
}

_ERROR_TOKENS: Final = frozenset(
    {"error", "status_code_error", "2", "unset_error", "exception"}
)


class BaseAdapter(ABC):
    """所有适配器的基类。子类只需实现 adapt()。"""

    @abstractmethod
    def adapt(self, raw: dict[str, object], project_id: UUID) -> AriadneSpan:
        """把单条原始记录归一化为 AriadneSpan。"""

    @staticmethod
    def s(data: dict[str, Any], *keys: str, default: str = "") -> str:
        """按顺序取第一个非空值并转字符串。"""
        for key in keys:
            value = data.get(key)
            if value not in (None, ""):
                return str(value)
        return default

    @staticmethod
    def i(data: dict[str, Any], *keys: str, default: int = 0) -> int:
        """按顺序取第一个可转 int 的值。字符串数字也接受。"""
        for key in keys:
            value = data.get(key)
            if value is None or value == "":
                continue
            try:
                return int(float(value))
            except (TypeError, ValueError):
                continue
        return default

    @staticmethod
    def parse_time(value: object) -> datetime:
        """接受 ISO 字符串、秒/毫秒/纳秒时间戳；无法解析时回退到当前时间。

        单位靠数量级判定：OTLP 用纳秒，多数 SDK 用秒或毫秒，
        混在一起时数量级是唯一可靠线索。
        """
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)

        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                value = int(text)
            else:
                try:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
                except ValueError:
                    return datetime.now(UTC)

        if isinstance(value, (int, float)):
            num = float(value)
            if num <= 0:
                return datetime.now(UTC)
            if num >= _NS_THRESHOLD:
                num /= NANOS_PER_SEC
            elif num >= _MS_THRESHOLD:
                num /= 1000.0
            elif num < _SEC_THRESHOLD:
                return datetime.now(UTC)
            try:
                return datetime.fromtimestamp(num, tz=UTC)
            except (OverflowError, OSError, ValueError):
                return datetime.now(UTC)

        return datetime.now(UTC)

    def trace_id(self, data: dict[str, Any], *keys: str) -> str:
        """取并归一化 trace_id。无法识别时新生成，避免整条 span 被丢弃。"""
        from ariadne.utils.ids import new_trace_id

        normalized = normalize_trace_id(self.s(data, *keys))
        return normalized or new_trace_id()

    def span_id(self, data: dict[str, Any], *keys: str) -> str:
        from ariadne.utils.ids import new_span_id

        normalized = normalize_span_id(self.s(data, *keys))
        return normalized or new_span_id()

    def parent_id(self, data: dict[str, Any], *keys: str) -> str:
        """父 ID 无法识别时返回空（表示根 span），不能瞎生成。"""
        return normalize_span_id(self.s(data, *keys))

    @staticmethod
    def normalize_kind(text: str) -> SpanKind:
        return _KIND_ALIASES.get(text.strip().lower(), SpanKind.INTERNAL)

    @staticmethod
    def normalize_status(text: str, *, has_error: bool = False) -> SpanStatus:
        lowered = text.strip().lower()
        if lowered == "blocked":
            return SpanStatus.BLOCKED
        if has_error or lowered in _ERROR_TOKENS or "error" in lowered:
            return SpanStatus.ERROR
        return SpanStatus.OK

    @staticmethod
    def flatten_attributes(attrs: object, prefix: str = "") -> dict[str, str]:
        """嵌套 dict/list 压平为点分键的平字典（存储层只接受 Map(String,String)）。"""
        flat: dict[str, str] = {}

        def walk(node: object, path: str) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    walk(value, f"{path}.{key}" if path else str(key))
            elif isinstance(node, (list, tuple)):
                for idx, value in enumerate(node):
                    walk(value, f"{path}.{idx}")
            elif node is not None:
                flat[path] = str(node)

        walk(attrs, prefix)
        return flat
