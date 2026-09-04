"""Ariadne 客户端：SDK 的主入口。"""

from __future__ import annotations

import os
from typing import Any, Literal

from ariadne_sdk.exporter import SpanExporter
from ariadne_sdk.span import Span, current_span

_DEFAULT_ENDPOINT = "http://localhost:8000/v1/ingest/spans"


class Ariadne:
    """埋点客户端。

    典型用法：
        client = Ariadne(api_key="...", project="my-app")
        with client.span("retrieve", kind="rag") as s:
            s.set_attributes({"top_k": 5})
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        project: str = "default",
        endpoint: str | None = None,
        batch_size: int = 100,
        flush_interval: float = 2.0,
        queue_size: int = 10_000,
        enabled: bool = True,
    ) -> None:
        self.project = project
        # enabled=False 用于测试与本地调试：所有 API 仍可调用但不上报
        self.enabled = enabled and bool(api_key or os.getenv("ARIADNE_API_KEY"))

        resolved_key: str = api_key or os.getenv("ARIADNE_API_KEY", "") or ""
        resolved_endpoint = (
            endpoint or os.getenv("ARIADNE_ENDPOINT") or _DEFAULT_ENDPOINT
        )

        self._exporter: SpanExporter | None = None
        if self.enabled:
            self._exporter = SpanExporter(
                endpoint=resolved_endpoint,
                api_key=resolved_key,
                batch_size=batch_size,
                flush_interval=flush_interval,
                queue_size=queue_size,
            )
            self._exporter.start()

    def span(
        self,
        name: str,
        *,
        kind: str = "internal",
        operation: str = "",
        provider: str = "",
        model: str = "",
        attributes: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> Span:
        """创建 span。作为上下文管理器使用时自动串联父子关系。"""
        span = Span(
            name=name,
            kind=kind,
            operation=operation,
            provider=provider,
            model=model,
            tags=list(tags or []),
            _exporter=self._exporter,
        )
        # 未进入 with 块时也要能继承父 span（用于手动 end() 的场景）
        parent = current_span()
        if parent is not None:
            span.trace_id = parent.trace_id
            span.parent_span_id = parent.span_id
        if attributes:
            span.set_attributes(attributes)
        return span

    def flush(self, timeout: float = 5.0) -> bool:
        return self._exporter.flush(timeout) if self._exporter else True

    def shutdown(self) -> None:
        if self._exporter is not None:
            self._exporter.shutdown()

    @property
    def stats(self) -> dict[str, int]:
        return self._exporter.stats.snapshot() if self._exporter else {}

    def __enter__(self) -> Ariadne:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: object,
    ) -> Literal[False]:
        self.shutdown()
        return False


_global_client: Ariadne | None = None


def init(api_key: str | None = None, **kwargs: Any) -> Ariadne:
    """初始化全局客户端，供 @trace 装饰器与自动埋点使用。"""
    global _global_client
    _global_client = Ariadne(api_key, **kwargs)
    return _global_client


def get_client() -> Ariadne | None:
    return _global_client
