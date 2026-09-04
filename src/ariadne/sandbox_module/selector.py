"""按配置选沙箱后端 —— 把 profile、后端、降级链连起来。

存在的理由：`SandboxProfile`、`ProfileSpec.isolation`、`SandboxFactory` 三者
建好后一直没有调用方，`settings.sandbox` 的五个旋钮无人读取，于是
`ProfileSpec.isolation` 说的"strict 档用 firecracker"从未生效 —— Loop 实际
一直走 `RestrictedRunner`（受限子进程），而它按自己的文档"防不住内核层逃逸"。

这里只负责**选后端并探测可用性**，不负责包装成 CommandRunner —— 那一步在
worker 侧，因为降级目标（RestrictedRunner）属于 loop_module，沙箱模块不该
反向依赖它。

降级链来自 firecracker.py 的既定声明（"不可用时降级到 gVisor 或受限子进程"）：
  firecracker → gvisor → None（由调用方降级到受限子进程或拒绝执行）
gvisor 不可用时不向上试 firecracker：后者的环境要求严格更多，不可能成立。
"""

from __future__ import annotations

from ariadne.config import SandboxSettings
from ariadne.sandbox_module.base import (
    BaseSandbox,
    SandboxProfile,
    SandboxUnavailableError,
)
from ariadne.sandbox_module.profiles import get_profile_spec
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

# 降级顺序：键不可用时依次尝试值里的后端
_DEGRADE_CHAIN: dict[str, tuple[str, ...]] = {
    "firecracker": ("gvisor",),
    "gvisor": (),
}


def resolve_profile(name: str) -> SandboxProfile:
    """settings 里的 profile 字符串转枚举。

    未知名称抛 ValueError 而非默认到 strict —— 把 `profile: strct` 这类拼写
    错误静默解释成某个档位，等于让人以为配了隔离而实际没配。
    """
    try:
        return SandboxProfile(name)
    except ValueError:
        known = ", ".join(p.value for p in SandboxProfile)
        raise ValueError(f"unknown sandbox profile {name!r}, known: {known}") from None


def backend_for_profile(profile: SandboxProfile) -> str:
    """该 profile 要求的隔离后端名（读 ProfileSpec.isolation）。"""
    return get_profile_spec(profile).isolation


def build_sandbox(settings: SandboxSettings) -> BaseSandbox | None:
    """按配置构造可用的沙箱后端；全不可用时返回 None。

    返回 None 不代表出错 —— 开发机上没有 KVM/runsc 是正常状态，
    调用方按 `fallback_to_restricted` 决定降级还是拒绝执行。

    profile 字符串非法时抛 ValueError（配置错误，不该静默吞掉）。
    """
    profile = resolve_profile(settings.profile)
    primary = backend_for_profile(profile)

    from ariadne.sandbox_module import SandboxFactory

    for backend in (primary, *_DEGRADE_CHAIN.get(primary, ())):
        # settings.image 是容器镜像，只有 gvisor 用；firecracker 走
        # rootfs/kernel 镜像（当前没有对应配置项，用类默认值）。
        kwargs: dict[str, object] = {"image": settings.image} if backend == "gvisor" else {}
        sandbox = SandboxFactory(backend, **kwargs)
        try:
            sandbox.probe()
        except SandboxUnavailableError as exc:
            logger.info(
                "沙箱后端不可用，尝试下一级",
                extra={"backend": backend, "profile": profile.value, "reason": str(exc)},
            )
            continue
        if backend != primary:
            logger.warning(
                "沙箱降级：profile 要求的后端不可用，已启用次级后端",
                extra={"profile": profile.value, "required": primary, "actual": backend},
            )
        return sandbox
    return None


__all__ = ["backend_for_profile", "build_sandbox", "resolve_profile"]
