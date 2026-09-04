"""沙箱三档 profile 规格（docs/04 §6）。

每档 profile 定义了隔离级别、资源限制、网络策略。
三档都不可关闭的强制项在 BaseSandbox 实现层保证：
  禁止宿主目录挂载、禁止提权、禁止云元数据访问、输出上限 1MB、用完即毁。
"""

from __future__ import annotations

from dataclasses import dataclass

from ariadne.sandbox_module.base import SandboxProfile


@dataclass(frozen=True)
class ProfileSpec:
    """单档 profile 的规格。

    network_deny=True 时完全禁网；network_allowlist 是允许的域名列表。
    三个档位的强制项（挂载/提权/元数据/输出/即毁）在沙箱实现层保证，
    不在此处配置 —— 它们是不可关闭的。
    """

    profile: SandboxProfile
    # 隔离级别：runsc（gVisor）/ firecracker（microVM）
    isolation: str
    # 网络
    network_deny: bool
    network_allowlist: tuple[str, ...]
    # CPU 限制
    cpu_cores: int
    cpu_seconds: int
    # 内存限制
    memory_bytes: int
    # 进程数限制
    max_processes: int
    # 可写路径（沙箱内）
    writable_paths: tuple[str, ...]
    # syscall 过滤模式：seccomp_whitelist / seccomp_default
    syscall_filter: str
    # root 文件系统是否只读
    root_readonly: bool


# docs/04 §6 三档 profile 表格
PROFILE_SPECS: dict[SandboxProfile, ProfileSpec] = {
    SandboxProfile.STRICT: ProfileSpec(
        profile=SandboxProfile.STRICT,
        isolation="firecracker",
        network_deny=True,
        network_allowlist=(),
        cpu_cores=1,
        cpu_seconds=30,
        memory_bytes=512 * 1024 * 1024,  # 512 MB
        max_processes=32,
        writable_paths=("/tmp",),
        syscall_filter="seccomp_whitelist",
        root_readonly=True,
    ),
    SandboxProfile.STANDARD: ProfileSpec(
        profile=SandboxProfile.STANDARD,
        isolation="gvisor",
        network_deny=False,
        network_allowlist=("api.anthropic.com", "api.openai.com"),
        cpu_cores=2,
        cpu_seconds=120,
        memory_bytes=2 * 1024 * 1024 * 1024,  # 2 GB
        max_processes=128,
        writable_paths=("/tmp", "/workspace"),
        syscall_filter="seccomp_whitelist",
        root_readonly=True,
    ),
    SandboxProfile.TRUSTED: ProfileSpec(
        profile=SandboxProfile.TRUSTED,
        isolation="gvisor",
        network_deny=False,
        network_allowlist=(),  # trusted 档允许内网
        cpu_cores=4,
        cpu_seconds=600,
        memory_bytes=8 * 1024 * 1024 * 1024,  # 8 GB
        max_processes=512,
        writable_paths=("/tmp", "/workspace", "/cache"),
        syscall_filter="seccomp_default",
        root_readonly=True,
    ),
}


def get_profile_spec(profile: SandboxProfile) -> ProfileSpec:
    """获取 profile 规格。"""
    return PROFILE_SPECS[profile]


__all__ = [
    "PROFILE_SPECS",
    "ProfileSpec",
    "get_profile_spec",
]
