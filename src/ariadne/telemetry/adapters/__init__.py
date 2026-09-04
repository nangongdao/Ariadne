"""适配层：把各家埋点格式归一化为内部 AriadneSpan 契约。

存在的理由：截至 2026 年，OTel GenAI semconv 中没有任何一个 gen_ai.* 属性
达到 Stable，且生态里并存 OpenInference / OpenLLMetry 两套事实标准。
把归一化收拢在这一层，上游改名只改映射，不迁移已入库的数据。
"""

from typing import TYPE_CHECKING
from uuid import UUID

from ariadne.telemetry.adapters.base import BaseAdapter
from ariadne.telemetry.models import AriadneSpan

if TYPE_CHECKING:
    from collections.abc import Callable

ADAPTER_REGISTRY: dict[str, type[BaseAdapter]] = {}

# 必须用独立标志而非"字典非空"判断是否已加载全部实现：
# 任何代码直接 import 某个适配器模块（如 ingest.py 引用 otlp 的工具函数）
# 都会让字典变成非空，从而让"非空即已加载"的判断提前返回，
# 导致其余适配器永远注册不上。
_loaded = False


def register_adapter(name: str) -> "Callable[[type[BaseAdapter]], type[BaseAdapter]]":
    def decorator(cls: type[BaseAdapter]) -> type[BaseAdapter]:
        ADAPTER_REGISTRY[name] = cls
        return cls

    return decorator


def AdapterFactory(name: str) -> BaseAdapter:  # noqa: N802
    """按格式名取适配器实例。未知格式显式报错，不静默回退。"""
    _ensure_loaded()
    if name not in ADAPTER_REGISTRY:
        known = ", ".join(sorted(ADAPTER_REGISTRY))
        raise ValueError(f"未知的遥测格式 {name!r}，已注册: {known}")
    return ADAPTER_REGISTRY[name]()


def available_formats() -> list[str]:
    _ensure_loaded()
    return sorted(ADAPTER_REGISTRY)


def adapt_batch(
    format_name: str, records: list[dict[str, object]], project_id: UUID
) -> tuple[list[AriadneSpan], list[str]]:
    """批量适配。单条失败不影响整批，返回 (成功列表, 错误摘要列表)。"""
    adapter = AdapterFactory(format_name)
    spans: list[AriadneSpan] = []
    errors: list[str] = []
    for idx, record in enumerate(records):
        try:
            spans.append(adapter.adapt(record, project_id))
        except Exception as exc:
            errors.append(f"#{idx}: {type(exc).__name__}: {exc}")
    return spans, errors


def _ensure_loaded() -> None:
    """延迟导入具体实现，避免包初始化时的循环导入。"""
    global _loaded
    if _loaded:
        return
    _loaded = True
    from ariadne.telemetry.adapters import (  # noqa: F401
        native,
        openinference,
        otlp,
    )


__all__ = [
    "ADAPTER_REGISTRY",
    "AdapterFactory",
    "BaseAdapter",
    "adapt_batch",
    "available_formats",
    "register_adapter",
]
