"""Loop 模式注册表。

独立成文件而非放在 __init__.py：__init__.py 在 _ensure_loaded 里 import
各模式子模块触发注册，各子模块又要 import register_loop_mode ——
拆开避免循环 import（telemetry.adapters / eval_module 用同一模式）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ariadne.loop_module.modes.base import BaseLoopMode, RetryDecision

if TYPE_CHECKING:
    from collections.abc import Callable

LOOP_MODE_REGISTRY: dict[str, type[BaseLoopMode]] = {}


def register_loop_mode(name: str) -> Callable[[type[BaseLoopMode]], type[BaseLoopMode]]:
    def decorator(cls: type[BaseLoopMode]) -> type[BaseLoopMode]:
        cls.name = name
        LOOP_MODE_REGISTRY[name] = cls
        return cls

    return decorator


__all__ = [
    "LOOP_MODE_REGISTRY",
    "BaseLoopMode",
    "RetryDecision",
    "register_loop_mode",
]
