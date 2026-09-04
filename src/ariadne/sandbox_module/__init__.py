"""Sandbox 模块 —— 沙箱隔离的工厂 + 注册。

SandboxFactory 按名称创建沙箱后端。默认注册 gvisor（生产用）。
gVisor 不可用时（Windows / 无 runsc），调用方应降级到受限子进程。

registry 模式与 loop_module.modes / eval_module 一致：
  register_sandbox(name) 装饰器注册实现类
  SandboxFactory(name) 返回实例
  _loaded 标志避免"dict 非空"反模式
"""

from __future__ import annotations

from ariadne.sandbox_module.base import (
    BaseSandbox,
    SandboxProfile,
    SandboxRunner,
    SandboxUnavailableError,
)
from ariadne.sandbox_module.firecracker import FirecrackerSandbox
from ariadne.sandbox_module.gvisor import GVisorSandbox
from ariadne.sandbox_module.pool import SandboxPool
from ariadne.sandbox_module.profiles import (
    PROFILE_SPECS,
    ProfileSpec,
    get_profile_spec,
)
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

SANDBOX_REGISTRY: dict[str, type[BaseSandbox]] = {}
_loaded = False


def register_sandbox(name: str) -> object:
    """注册沙箱后端。装饰器，标记类为可按 name 创建。"""

    def decorator(cls: type[BaseSandbox]) -> type[BaseSandbox]:
        cls.name = name  # type: ignore[attr-defined]
        SANDBOX_REGISTRY[name] = cls
        return cls

    return decorator


def _ensure_loaded() -> None:
    """确保具体后端已注册。

    两个后端：
    - gvisor：用户态内核隔离，Linux + runsc，中等安全
    - firecracker：真硬件虚拟化，Linux + KVM，最高安全（M6 §9 验收项 #10）
    """
    global _loaded
    if _loaded:
        return
    SANDBOX_REGISTRY["gvisor"] = GVisorSandbox
    SANDBOX_REGISTRY["firecracker"] = FirecrackerSandbox
    _loaded = True


def SandboxFactory(name: str, **kwargs: object) -> BaseSandbox:  # noqa: N802
    """按名称创建沙箱后端实例。

    未知名称抛 ValueError（列出已知名称），不静默 fallback。
    gVisor 不可用时由调用方决定降级策略。
    """
    _ensure_loaded()
    if name not in SANDBOX_REGISTRY:
        known = ", ".join(sorted(SANDBOX_REGISTRY))
        raise ValueError(f"unknown sandbox backend {name!r}, known: {known}")
    cls = SANDBOX_REGISTRY[name]
    return cls(**kwargs)


def available_backends() -> list[str]:
    """已注册的沙箱后端名称。"""
    _ensure_loaded()
    return sorted(SANDBOX_REGISTRY)


__all__ = [
    "PROFILE_SPECS",
    "SANDBOX_REGISTRY",
    "BaseSandbox",
    "FirecrackerSandbox",
    "GVisorSandbox",
    "ProfileSpec",
    "SandboxFactory",
    "SandboxPool",
    "SandboxProfile",
    "SandboxRunner",
    "SandboxUnavailableError",
    "available_backends",
    "get_profile_spec",
    "register_sandbox",
]
