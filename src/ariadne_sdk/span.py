"""Span 对象与上下文传播。

用 contextvars 而非 threading.local：async 场景下同一线程会交错运行多个协程，
threading.local 会导致父子关系错乱。
"""

from __future__ import annotations

import secrets
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal

if TYPE_CHECKING:
    from ariadne_sdk.exporter import SpanExporter

_MAX_PREVIEW: Final = 2048
_current_span: ContextVar[Span | None] = ContextVar("ariadne_current_span", default=None)


def _hex(n: int) -> str:
    return secrets.token_hex(n)


def current_span() -> Span | None:
    return _current_span.get()


def current_trace_id() -> str:
    span = _current_span.get()
    return span.trace_id if span else ""


@dataclass
class Span:
    """一次操作的记录。结束时序列化并交给 exporter 入队。"""

    name: str
    kind: str = "internal"
    trace_id: str = field(default_factory=lambda: _hex(16))
    span_id: str = field(default_factory=lambda: _hex(8))
    parent_span_id: str = ""
    operation: str = ""
    provider: str = ""
    model: str = ""
    model_response: str = ""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0

    input_preview: str = ""
    output_preview: str = ""
    attributes: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)

    status: str = "ok"
    error_type: str = ""

    _exporter: SpanExporter | None = field(default=None, repr=False)
    _started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    _start_perf: float = field(default_factory=time.perf_counter)
    _token: Token[Span | None] | None = field(default=None, repr=False)
    _ended: bool = field(default=False, repr=False)

    def set_attributes(self, values: dict[str, Any]) -> Span:
        for key, value in values.items():
            if value is not None:
                self.attributes[str(key)] = str(value)[:1024]
        return self

    def set_usage(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
    ) -> Span:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_tokens = cache_read_tokens
        self.cache_write_tokens = cache_write_tokens
        self.reasoning_tokens = reasoning_tokens
        return self

    def set_input(self, text: str) -> Span:
        self.input_preview = str(text)[:_MAX_PREVIEW]
        return self

    def set_output(self, text: str) -> Span:
        self.output_preview = str(text)[:_MAX_PREVIEW]
        return self

    def set_model(self, model: str, *, response_model: str = "", provider: str = "") -> Span:
        self.model = model
        self.model_response = response_model or model
        if provider:
            self.provider = provider
        return self

    def record_error(self, exc: BaseException) -> Span:
        self.status = "error"
        self.error_type = type(exc).__name__
        return self

    def __enter__(self) -> Span:
        parent = _current_span.get()
        if parent is not None:
            # 继承 trace，串成父子；显式设置过 parent 的不覆盖
            self.trace_id = parent.trace_id
            if not self.parent_span_id:
                self.parent_span_id = parent.span_id
        self._token = _current_span.set(self)
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        exc: BaseException | None,
        _tb: object,
    ) -> Literal[False]:
        if exc is not None:
            self.record_error(exc)
        self.end()
        return False  # 不吞异常

    def end(self) -> None:
        """结束并入队。幂等：重复调用不会重复上报。"""
        if self._ended:
            return
        self._ended = True

        if self._token is not None:
            _current_span.reset(self._token)
            self._token = None

        if self._exporter is not None:
            self._exporter.submit(self.to_payload())

    @property
    def duration_ms(self) -> int:
        return int((time.perf_counter() - self._start_perf) * 1000)

    def to_payload(self) -> dict[str, Any]:
        """转为 native 格式。不含 cost —— 成本一律由服务端计算。"""
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "kind": self.kind,
            "operation": self.operation,
            "provider": self.provider,
            "model": self.model,
            "model_response": self.model_response,
            "started_at": self._started_at.isoformat(),
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error_type": self.error_type,
            "usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens,
                "cache_write_tokens": self.cache_write_tokens,
                "reasoning_tokens": self.reasoning_tokens,
            },
            "input_preview": self.input_preview,
            "output_preview": self.output_preview,
            "attributes": self.attributes,
            "tags": self.tags,
        }
