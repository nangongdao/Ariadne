"""Loop 模式注册表与工厂。

与 telemetry.adapters / eval_module / verifier 同构。同样用独立加载标志
而非"字典非空"判定——直接 import 子模块的代码会把其余实现挡在注册表外。
"""

from __future__ import annotations

from ariadne.loop_module.modes.base import BaseLoopMode, RetryDecision
from ariadne.loop_module.modes.registry import (
    LOOP_MODE_REGISTRY,
    register_loop_mode,
)


def LoopModeFactory(name: str) -> BaseLoopMode:  # noqa: N802
    """按名取模式实例。未注册名显式报错。"""
    _ensure_loaded()
    if name not in LOOP_MODE_REGISTRY:
        known = ", ".join(sorted(LOOP_MODE_REGISTRY))
        raise ValueError(f"未注册的 loop 模式 {name!r}，已支持: {known}")
    return LOOP_MODE_REGISTRY[name]()


def available_modes() -> list[str]:
    _ensure_loaded()
    return sorted(LOOP_MODE_REGISTRY)


_loaded = False


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    # 触发各模式的 @register_loop_mode 装饰器
    from ariadne.loop_module.modes import (  # noqa: F401
        hitl,
        quality,
        retry,
        verify_execute,
    )


__all__ = [
    "LOOP_MODE_REGISTRY",
    "BaseLoopMode",
    "LoopModeFactory",
    "RetryDecision",
    "available_modes",
    "register_loop_mode",
]
