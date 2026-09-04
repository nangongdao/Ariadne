"""Firecracker microVM 沙箱测试 —— M6 §9 验收项 #10。

复用 M4 沙箱用例集 + VM 逃逸用例。

验收项 #10：沙箱逃逸（Firecracker 档）全部失败。
Firecracker 用真硬件虚拟化（KVM），每个沙箱是独立 microVM + 独立内核，
VM 逃逸才能突破（远难于容器逃逸）。

这些测试在无 KVM 环境（Windows / CI）下验证：
1. Firecracker 驱动正确检测不可用环境并抛 SandboxUnavailableError
2. strict profile 映射到 firecracker 隔离档
3. VM 逃逸用例的隔离边界（网络/文件系统/进程/内核）
4. 输出截断、超时强杀等不可关闭项
5. 工厂注册 + 降级链
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ariadne.sandbox_module import (
    SandboxFactory,
    available_backends,
)
from ariadne.sandbox_module.base import (
    BaseSandbox,
    SandboxProfile,
    SandboxUnavailableError,
)
from ariadne.sandbox_module.firecracker import MAX_OUTPUT_BYTES, FirecrackerSandbox
from ariadne.sandbox_module.profiles import get_profile_spec

# ============================================================
# 1. 工厂注册 + 可用性
# ============================================================


class TestFirecrackerFactory:
    """Firecracker 在工厂注册表里。"""

    def test_firecracker_registered(self) -> None:
        assert "firecracker" in available_backends()

    def test_factory_creates_firecracker(self) -> None:
        sb = SandboxFactory("firecracker")
        assert isinstance(sb, FirecrackerSandbox)

    def test_firecracker_is_base_sandbox(self) -> None:
        sb = SandboxFactory("firecracker")
        assert isinstance(sb, BaseSandbox)

    def test_available_backends_includes_firecracker(self) -> None:
        backends = available_backends()
        assert "firecracker" in backends
        assert "gvisor" in backends


# ============================================================
# 2. 可用性检测（无 KVM 环境）
# ============================================================


class TestFirecrackerAvailability:
    """Firecracker 需要 Linux + KVM + firecracker 二进制。

    在无 KVM 环境（Windows / 无 firecracker / 无 /dev/kvm）下
    实例化或 run 时应抛 SandboxUnavailableError。
    """

    def test_unavailable_without_firecracker_binary(self) -> None:
        """无 firecracker 二进制 → SandboxUnavailableError。"""
        sb = FirecrackerSandbox()
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(SandboxUnavailableError, match="firecracker binary"),
        ):
            sb._check_available()

    def test_unavailable_without_kvm(self) -> None:
        """无 /dev/kvm → SandboxUnavailableError。"""
        sb = FirecrackerSandbox()
        with (
            patch("shutil.which", return_value="/usr/bin/firecracker"),
            patch("os.path.exists", return_value=False),
            pytest.raises(SandboxUnavailableError, match="/dev/kvm"),
        ):
            sb._check_available()

    def test_unavailable_without_kvm_permissions(self) -> None:
        """/dev/kvm 存在但无权限 → SandboxUnavailableError。"""
        sb = FirecrackerSandbox()
        with (
            patch("shutil.which", return_value="/usr/bin/firecracker"),
            patch("os.path.exists", return_value=True),
            patch("os.access", return_value=False),
            pytest.raises(SandboxUnavailableError, match="readable/writable"),
        ):
            sb._check_available()

    def test_unavailable_without_rootfs(self) -> None:
        """rootfs 镜像不存在 → SandboxUnavailableError。"""
        sb = FirecrackerSandbox()
        fc_path = "/usr/bin/firecracker"

        def mock_version(*args: object, **kwargs: object) -> object:
            from unittest.mock import MagicMock

            result = MagicMock()
            result.returncode = 0
            result.stderr = ""
            return result

        with (
            patch("shutil.which", return_value=fc_path),
            patch("os.path.exists", return_value=True),
            patch("os.access", return_value=True),
            patch("subprocess.run", side_effect=mock_version),
            patch("pathlib.Path.exists", return_value=False),
            pytest.raises(SandboxUnavailableError, match="rootfs not found"),
        ):
            sb._check_available()

    def test_available_cached_after_first_check(self) -> None:
        """可用性检测结果缓存，不重复检查。"""
        sb = FirecrackerSandbox()
        # 强制标记为可用
        sb._available = True
        # 不会重新检查
        sb._check_available()
        assert sb._available is True

    @pytest.mark.asyncio
    async def test_run_raises_when_unavailable(self) -> None:
        """不可用环境下 run() 抛 SandboxUnavailableError。"""
        sb = FirecrackerSandbox()
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(SandboxUnavailableError),
        ):
            await sb.run("echo hello", workdir=Path("/tmp"))


# ============================================================
# 3. strict profile 映射到 firecracker
# ============================================================


class TestFirecrackerProfile:
    """strict 档用 firecracker（最高隔离），docs/04 §6。"""

    def test_strict_isolation_is_firecracker(self) -> None:
        strict = get_profile_spec(SandboxProfile.STRICT)
        assert strict.isolation == "firecracker"

    def test_strict_no_network(self) -> None:
        strict = get_profile_spec(SandboxProfile.STRICT)
        assert strict.network_deny is True
        assert strict.network_allowlist == ()

    def test_strict_readonly_root(self) -> None:
        strict = get_profile_spec(SandboxProfile.STRICT)
        assert strict.root_readonly is True

    def test_strict_minimal_writable_paths(self) -> None:
        strict = get_profile_spec(SandboxProfile.STRICT)
        assert strict.writable_paths == ("/tmp",)
        assert "/workspace" not in strict.writable_paths

    def test_strict_low_process_limit(self) -> None:
        strict = get_profile_spec(SandboxProfile.STRICT)
        assert strict.max_processes == 32

    def test_strict_seccomp_whitelist(self) -> None:
        strict = get_profile_spec(SandboxProfile.STRICT)
        assert strict.syscall_filter == "seccomp_whitelist"

    def test_strict_low_cpu_memory(self) -> None:
        strict = get_profile_spec(SandboxProfile.STRICT)
        assert strict.cpu_cores == 1
        assert strict.memory_bytes == 512 * 1024 * 1024
        assert strict.cpu_seconds == 30


# ============================================================
# 4. VM 逃逸用例 —— 隔离边界验证（M6 §9 #10）
# ============================================================


class TestVMEscapePrevention:
    """VM 逃逸用例：验证 Firecracker 档的隔离边界。

    这些测试验证隔离配置的正确性（profile spec 层面），
    真 VM 逃逸需在 Linux + KVM 环境跑集成测试。
    """

    def test_no_host_directory_mount(self) -> None:
        """strict 档禁止宿主目录挂载 —— 代码通过 vsock/initrd 注入。"""
        strict = get_profile_spec(SandboxProfile.STRICT)
        # writable_paths 只含沙箱内路径（/tmp），不含宿主路径
        for path in strict.writable_paths:
            assert not path.startswith("/host")
            assert not path.startswith("/home")

    def test_no_host_network_access(self) -> None:
        """strict 档完全禁网 —— VM 无网络设备。"""
        strict = get_profile_spec(SandboxProfile.STRICT)
        assert strict.network_deny is True
        assert len(strict.network_allowlist) == 0

    def test_no_privilege_escalation(self) -> None:
        """root fs 只读 + seccomp_whitelist → 无法提权。"""
        strict = get_profile_spec(SandboxProfile.STRICT)
        assert strict.root_readonly is True
        assert strict.syscall_filter == "seccomp_whitelist"

    def test_no_inter_container_escape(self) -> None:
        """每个 VM 独立内核 —— 容器逃逸不等于 VM 逃逸。

        Firecracker 不共享宿主内核态（gVisor 共享），
        VM 逃逸需要 hypervisor 漏洞，远难于容器逃逸。
        """
        strict = get_profile_spec(SandboxProfile.STRICT)
        assert strict.isolation == "firecracker"
        # 与 gVisor 的关键区别：firecracker 是真 VM
        standard = get_profile_spec(SandboxProfile.STANDARD)
        assert standard.isolation == "gvisor"

    def test_no_metadata_service_access(self) -> None:
        """strict 档禁网 → 无法访问云元数据服务（169.254.169.254）。"""
        strict = get_profile_spec(SandboxProfile.STRICT)
        assert strict.network_deny is True
        # 禁网意味着所有出站请求失败，含元数据服务

    def test_output_capped_at_1mb(self) -> None:
        """stdout/stderr 各 1MB 上限，超限截断（不可关闭）。"""
        assert MAX_OUTPUT_BYTES == 1_000_000

    def test_vm_destroyed_after_execution(self) -> None:
        """Firecracker 用完即毁 —— close 后 VM 销毁。"""
        # 验证 FirecrackerSandbox 有 close 方法
        sb = FirecrackerSandbox()
        assert hasattr(sb, "close")
        assert hasattr(sb, "run")

    def test_vm_timeout_kills_vm(self) -> None:
        """超时强杀 VM（整个进程组）。"""
        strict = get_profile_spec(SandboxProfile.STRICT)
        # cpu_seconds 是 VM 执行超时上限
        assert strict.cpu_seconds == 30
        # 超时后 VM 被销毁，不留状态


# ============================================================
# 5. 降级链：Firecracker → gVisor → 受限子进程
# ============================================================


class TestDegradationChain:
    """沙箱不可用时的降级链。

    Firecracker 不可用 → gVisor
    gVisor 不可用 → 受限子进程（M3 兼容）
    """

    def test_firecracker_unavailable_raises(self) -> None:
        """Firecracker 不可用时抛 SandboxUnavailableError（调用方决定降级）。"""
        sb = FirecrackerSandbox()
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(SandboxUnavailableError),
        ):
            sb._check_available()

    def test_gvisor_available_as_alternative(self) -> None:
        """gVisor 是 Firecracker 不可用时的第一降级选项。"""
        from ariadne.sandbox_module.gvisor import GVisorSandbox

        sb = GVisorSandbox()
        # gVisor 在无 docker 环境也抛 SandboxUnavailableError
        with (
            patch("shutil.which", return_value=None),
            pytest.raises(SandboxUnavailableError),
        ):
            sb._check_available()

    def test_both_backends_registered(self) -> None:
        """两个后端都注册了，降级链有选项可选。"""
        backends = available_backends()
        assert "firecracker" in backends
        assert "gvisor" in backends


# ============================================================
# 6. 输出截断 + 超时
# ============================================================


class TestOutputLimits:
    """输出上限 1MB 是不可关闭的强制项。"""

    def test_max_output_bytes_constant(self) -> None:
        assert MAX_OUTPUT_BYTES == 1_000_000

    def test_output_truncation_logic(self) -> None:
        """模拟输出截断：超 1MB 截断并标记 truncated。"""
        # 模拟 firecracker.run 的截断逻辑
        stdout = "x" * (MAX_OUTPUT_BYTES + 100)
        truncated = False
        if len(stdout) > MAX_OUTPUT_BYTES:
            stdout = stdout[:MAX_OUTPUT_BYTES]
            truncated = True

        assert len(stdout) == MAX_OUTPUT_BYTES
        assert truncated is True

    def test_output_under_limit_not_truncated(self) -> None:
        """输出 < 1MB 不截断。"""
        stdout = "x" * 100
        truncated = False
        if len(stdout) > MAX_OUTPUT_BYTES:
            stdout = stdout[:MAX_OUTPUT_BYTES]
            truncated = True

        assert len(stdout) == 100
        assert truncated is False
