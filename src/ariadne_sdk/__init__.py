"""Ariadne Python SDK。

最小依赖（仅 httpx + pydantic），可独立安装，不拉服务端依赖。
"""

from ariadne_sdk.client import Ariadne, get_client, init
from ariadne_sdk.decorators import trace
from ariadne_sdk.exporter import ExporterStats, SpanExporter
from ariadne_sdk.span import Span, current_span, current_trace_id

__version__ = "0.1.0"

__all__ = [
    "Ariadne",
    "ExporterStats",
    "Span",
    "SpanExporter",
    "__version__",
    "current_span",
    "current_trace_id",
    "get_client",
    "init",
    "trace",
]
