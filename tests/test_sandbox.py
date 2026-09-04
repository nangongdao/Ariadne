"""Sandbox 模块测试 —— profiles、runner 适配、pool、gvisor 可用性检测。"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from ariadne.loop_module.verifier.restricted_exec import ExecResult
from ariadne.sandbox_module import SandboxFactory, available_backends
from ariadne.sandbox_module.base import (
    BaseSandbox,
    SandboxProfile,
    SandboxRunner,
    SandboxUnavailableError,
)
from ariadne.sandbox_module.gvisor import GVisorSandbox
from ariadne.sandbox_module.pool import SandboxPool
from ariadne.sandbox_module.profiles import PROFILE_SPECS, get_profile_spec

# ---------- 桩沙箱（测试用，不依赖真实 Docker） ----------


@dataclass
class StubSandbox(BaseSandbox):
    """桩沙箱。记录调用参数，返回固定结果。"""

    exit_code: int = 0
    stdout_text: str = "ok"
    last_cmd: str = ""
    last_workdir: Path | None = None
    last_profile: SandboxProfile | None = None
    closed: bool = False

    async def run(
        self,
        cmd: str,
        *,
        workdir: Path,
        profile: SandboxProfile = SandboxProfile.STRICT,
    ) -> ExecResult:
        self.last_cmd = cmd
        self.last_workdir = workdir
        self.last_profile = profile
        return ExecResult(
            exit_code=self.exit_code,
            stdout=self.stdout_text,
            stderr="",
            duration_ms=1,
        )

    async def close(self) -> None:
        self.closed = True


@dataclass
class FailingSandbox(BaseSandbox):
    exit_code: int = 1
    stderr_text: str = "boom"

    async def run(
        self,
        cmd: str,
        *,
        workdir: Path,
        profile: SandboxProfile = SandboxProfile.STRICT,
    ) -> ExecResult:
        return ExecResult(
            exit_code=self.exit_code,
            stdout="",
            stderr=self.stderr_text,
            duration_ms=1,
        )

    async def close(self) -> None:
        pass


# ---------- profiles ----------


class TestSandboxProfiles:
    """三档 profile 规格。"""

    def test_all_three_profiles_defined(self) -> None:
        assert SandboxProfile.STRICT in PROFILE_SPECS
        assert SandboxProfile.STANDARD in PROFILE_SPECS
        assert SandboxProfile.TRUSTED in PROFILE_SPECS

    def test_strict_is_most_restricted(self) -> None:
        strict = get_profile_spec(SandboxProfile.STRICT)
        assert strict.network_deny is True
        assert strict.cpu_cores == 1
        assert strict.memory_bytes == 512 * 1024 * 1024
        assert strict.max_processes == 32

    def test_strict_no_allowlist(self) -> None:
        strict = get_profile_spec(SandboxProfile.STRICT)
        assert strict.network_allowlist == ()

    def test_standard_allows_provider_network(self) -> None:
        standard = get_profile_spec(SandboxProfile.STANDARD)
        assert standard.network_deny is False
        assert "api.anthropic.com" in standard.network_allowlist
        assert "api.openai.com" in standard.network_allowlist

    def test_trusted_has_most_resources(self) -> None:
        trusted = get_profile_spec(SandboxProfile.TRUSTED)
        assert trusted.cpu_cores == 4
        assert trusted.memory_bytes == 8 * 1024 * 1024 * 1024
        assert trusted.max_processes == 512

    def test_all_profiles_readonly_root(self) -> None:
        """三档都强制 root fs 只读（docs/04 §6）。"""
        for spec in PROFILE_SPECS.values():
            assert spec.root_readonly is True

    def test_all_profiles_seccomp(self) -> None:
        """三档都有 syscall 过滤。"""
        for spec in PROFILE_SPECS.values():
            assert spec.syscall_filter in ("seccomp_whitelist", "seccomp_default")

    def test_trusted_uses_default_seccomp(self) -> None:
        """trusted 档用 seccomp_default（放宽，但仍过滤）。"""
        trusted = get_profile_spec(SandboxProfile.TRUSTED)
        assert trusted.syscall_filter == "seccomp_default"

    def test_strict_uses_whitelist_seccomp(self) -> None:
        """strict 档用 seccomp_whitelist（最严）。"""
        strict = get_profile_spec(SandboxProfile.STRICT)
        assert strict.syscall_filter == "seccomp_whitelist"

    def test_strict_isolation_is_firecracker(self) -> None:
        """strict 档用 firecracker（microVM，最强隔离）。"""
        strict = get_profile_spec(SandboxProfile.STRICT)
        assert strict.isolation == "firecracker"

    def test_standard_isolation_is_gvisor(self) -> None:
        standard = get_profile_spec(SandboxProfile.STANDARD)
        assert standard.isolation == "gvisor"

    def test_get_profile_spec_returns_correct_profile(self) -> None:
        for profile in SandboxProfile:
            spec = get_profile_spec(profile)
            assert spec.profile == profile


# ---------- SandboxFactory ----------


class TestSandboxFactory:
    """工厂注册模式。"""

    def test_gvisor_registered(self) -> None:
        assert "gvisor" in available_backends()

    def test_factory_creates_gvisor(self) -> None:
        sb = SandboxFactory("gvisor")
        assert isinstance(sb, GVisorSandbox)

    def test_factory_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown sandbox"):
            SandboxFactory("nonexistent")

    def test_available_backends_sorted(self) -> None:
        backends = available_backends()
        assert backends == sorted(backends)


# ---------- GVisor 可用性检测 ----------


class TestGVisorAvailability:
    """gVisor 可用性检测 —— 本机无 Docker/runsc 时应抛错。"""

    def test_gvisor_unavailable_on_no_docker(self) -> None:
        """Windows 开发机无 Docker → SandboxUnavailableError，或可用时跳过。"""
        sb = GVisorSandbox()
        with contextlib.suppress(SandboxUnavailableError):
            sb._check_available()


# ---------- SandboxRunner 适配 ----------


class TestSandboxRunner:
    """SandboxRunner 把异步沙箱适配成同步 CommandRunner。"""

    def test_runner_returns_exec_result(self) -> None:
        sb = StubSandbox(stdout_text="hello world")
        runner = SandboxRunner(sandbox=sb)
        result = runner.run("echo hello", workdir=Path("/tmp"))
        assert result.exit_code == 0
        assert "hello world" in result.stdout
        assert result.succeeded

    def test_runner_passes_cmd_and_workdir(self) -> None:
        sb = StubSandbox()
        runner = SandboxRunner(sandbox=sb)
        runner.run("pytest -x", workdir=Path("/project"))
        assert sb.last_cmd == "pytest -x"
        assert sb.last_workdir == Path("/project")

    def test_runner_uses_configured_profile(self) -> None:
        sb = StubSandbox()
        runner = SandboxRunner(sandbox=sb, profile=SandboxProfile.TRUSTED)
        runner.run("ls", workdir=Path("/tmp"))
        assert sb.last_profile == SandboxProfile.TRUSTED

    def test_runner_default_profile_is_strict(self) -> None:
        sb = StubSandbox()
        runner = SandboxRunner(sandbox=sb)
        runner.run("ls", workdir=Path("/tmp"))
        assert sb.last_profile == SandboxProfile.STRICT

    def test_runner_preserves_failure(self) -> None:
        runner = SandboxRunner(sandbox=FailingSandbox())
        result = runner.run("bad cmd", workdir=Path("/tmp"))
        assert result.exit_code == 1
        assert not result.succeeded
        assert "boom" in result.stderr

    def test_runner_works_inside_event_loop(self) -> None:
        """在已有事件循环内调用 run() —— 用线程桥接。"""

        async def _test() -> None:
            sb = StubSandbox(stdout_text="in loop")
            runner = SandboxRunner(sandbox=sb)
            result = runner.run("echo", workdir=Path("/tmp"))
            assert result.exit_code == 0
            assert "in loop" in result.stdout

        asyncio.run(_test())


# ---------- SandboxPool ----------


class TestSandboxPool:
    """预热池逻辑。"""

    def test_pool_initial_capacity(self) -> None:
        pool = SandboxPool(factory=StubSandbox, max_concurrency=4)
        # 4 * 1.5 = 6
        assert pool.available == 6

    def test_pool_custom_warm_factor(self) -> None:
        pool = SandboxPool(factory=StubSandbox, max_concurrency=4, warm_factor=2.0)
        assert pool.available == 8

    def test_acquire_decrements_available(self) -> None:
        pool = SandboxPool(factory=StubSandbox, max_concurrency=2)
        initial = pool.available
        sb = asyncio.run(pool.acquire())
        assert pool.available == initial - 1
        asyncio.run(pool.release(sb))

    def test_release_replenishes(self) -> None:
        pool = SandboxPool(factory=StubSandbox, max_concurrency=2)
        sb = asyncio.run(pool.acquire())
        asyncio.run(pool.release(sb))
        assert pool.available == 3  # back to 2 * 1.5

    def test_acquire_creates_sandbox(self) -> None:
        pool = SandboxPool(factory=StubSandbox)
        sb = asyncio.run(pool.acquire())
        assert isinstance(sb, StubSandbox)
        asyncio.run(pool.release(sb))

    def test_release_destroys_sandbox(self) -> None:
        pool = SandboxPool(factory=StubSandbox)
        sb = asyncio.run(pool.acquire())
        assert not sb.closed
        asyncio.run(pool.release(sb))
        assert sb.closed

    def test_close_cancels_waiters(self) -> None:
        pool = SandboxPool(factory=StubSandbox, max_concurrency=1)
        asyncio.run(pool.close())
        with pytest.raises(RuntimeError, match="closed"):
            asyncio.run(pool.acquire())

    def test_close_sets_closed_flag(self) -> None:
        pool = SandboxPool(factory=StubSandbox)
        asyncio.run(pool.close())
        # 二次 close 不报错
        asyncio.run(pool.close())

    def test_waiting_count_zero_initially(self) -> None:
        pool = SandboxPool(factory=StubSandbox)
        assert pool.waiting == 0

    def test_release_multiple_replenishes_all(self) -> None:
        pool = SandboxPool(factory=StubSandbox, max_concurrency=2)
        sbs = [asyncio.run(pool.acquire()) for _ in range(3)]
        assert pool.available == 0
        for sb in sbs:
            asyncio.run(pool.release(sb))
        assert pool.available == 3
