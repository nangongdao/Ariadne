"""沙箱选型与降级 —— 覆盖"配了沙箱到底会不会生效"这条路径。

为什么这组测试值得单独存在：sandbox_module 的工厂、profile、两个后端全都
建好了，但在此之前**没有任何生产代码调用它们** —— `engine._build_command_runner`
硬编码 RestrictedRunner，`settings.sandbox` 五个旋钮无人读取。于是
`ProfileSpec.isolation` 声明的"strict 档用 firecracker"从未生效过，
而受限子进程按自己的文档"防不住内核层逃逸"。

本机（Windows，无 KVM/runsc）只能验证**选型与降级决策**，验证不了真隔离
效果 —— 后者需要 Linux + runsc，是 M4 验收项 2 仍未验证的部分。这个边界
是刻意画出来的：把决策逻辑测死，比假装测了隔离更有用。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ariadne.config import SandboxSettings, Settings
from ariadne.sandbox_module.base import (
    BaseSandbox,
    SandboxProfile,
    SandboxRunner,
    SandboxUnavailableError,
)
from ariadne.sandbox_module.selector import (
    backend_for_profile,
    build_sandbox,
    resolve_profile,
)


class _FakeSandbox(BaseSandbox):
    """总是可用的桩后端。probe 不抛错即视为可用。"""

    async def run(self, cmd, *, workdir, profile=SandboxProfile.STRICT):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def close(self) -> None:
        return None


class _UnavailableSandbox(BaseSandbox):
    async def run(self, cmd, *, workdir, profile=SandboxProfile.STRICT):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def close(self) -> None:
        return None

    def probe(self) -> None:
        raise SandboxUnavailableError("桩：不可用")


class TestProfileDrivesBackend:
    """ProfileSpec.isolation 决定后端 —— 这个字段此前无人读取。"""

    def test_strict_requires_firecracker(self) -> None:
        """strict 是默认档，它要求的是硬件虚拟化而非 gVisor。"""
        assert backend_for_profile(SandboxProfile.STRICT) == "firecracker"

    @pytest.mark.parametrize("profile", [SandboxProfile.STANDARD, SandboxProfile.TRUSTED])
    def test_relaxed_profiles_use_gvisor(self, profile: SandboxProfile) -> None:
        assert backend_for_profile(profile) == "gvisor"

    def test_every_profile_maps_to_a_registered_backend(self) -> None:
        """新增 profile 却忘了注册对应后端，会在装配时才炸。"""
        from ariadne.sandbox_module import available_backends

        known = set(available_backends())
        for profile in SandboxProfile:
            assert backend_for_profile(profile) in known, f"{profile} 指向未注册后端"


class TestProfileResolution:
    def test_valid_names_round_trip(self) -> None:
        for profile in SandboxProfile:
            assert resolve_profile(profile.value) is profile

    def test_typo_raises_instead_of_defaulting(self) -> None:
        """拼错静默回落到某个档位，等于让人以为配了隔离而实际没配。"""
        with pytest.raises(ValueError, match="unknown sandbox profile"):
            resolve_profile("strct")

    def test_error_lists_known_profiles(self) -> None:
        with pytest.raises(ValueError, match="strict"):
            resolve_profile("")


class TestDegradeChain:
    """firecracker → gvisor → None，来自 firecracker.py 的既定声明。"""

    def test_falls_back_to_gvisor_when_firecracker_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        def fake_factory(name: str, **kwargs: object) -> BaseSandbox:
            seen.append(name)
            return _UnavailableSandbox() if name == "firecracker" else _FakeSandbox()

        monkeypatch.setattr("ariadne.sandbox_module.SandboxFactory", fake_factory)
        sandbox = build_sandbox(SandboxSettings(profile="strict"))
        assert isinstance(sandbox, _FakeSandbox)
        assert seen == ["firecracker", "gvisor"], "降级顺序错了"

    def test_gvisor_profile_does_not_escalate_to_firecracker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """向上试更严格的后端不可能成立，试了只是白费一次探测。"""
        seen: list[str] = []

        def fake_factory(name: str, **kwargs: object) -> BaseSandbox:
            seen.append(name)
            return _UnavailableSandbox()

        monkeypatch.setattr("ariadne.sandbox_module.SandboxFactory", fake_factory)
        assert build_sandbox(SandboxSettings(profile="standard")) is None
        assert seen == ["gvisor"]

    def test_returns_none_when_all_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ariadne.sandbox_module.SandboxFactory",
            lambda name, **kw: _UnavailableSandbox(),
        )
        assert build_sandbox(SandboxSettings(profile="strict")) is None

    def test_image_setting_only_reaches_container_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """firecracker 走 rootfs/kernel，传 image 会 TypeError。"""
        captured: dict[str, dict[str, object]] = {}

        def fake_factory(name: str, **kwargs: object) -> BaseSandbox:
            captured[name] = kwargs
            return _UnavailableSandbox() if name == "firecracker" else _FakeSandbox()

        monkeypatch.setattr("ariadne.sandbox_module.SandboxFactory", fake_factory)
        build_sandbox(SandboxSettings(profile="strict", image="custom/img:1"))
        assert captured["firecracker"] == {}
        assert captured["gvisor"] == {"image": "custom/img:1"}

    def test_real_backends_accept_what_selector_passes(self) -> None:
        """上一条用的是桩工厂，这条盯真实签名 —— 改构造参数会在此暴露。"""
        import dataclasses

        from ariadne.sandbox_module.firecracker import FirecrackerSandbox
        from ariadne.sandbox_module.gvisor import GVisorSandbox

        gvisor_fields = {f.name for f in dataclasses.fields(GVisorSandbox) if f.init}
        fc_fields = {f.name for f in dataclasses.fields(FirecrackerSandbox) if f.init}
        assert "image" in gvisor_fields
        assert "image" not in fc_fields


class TestUnavailabilitySurfacesAtWiringTime:
    """装配时探测，而不是等 Loop 跑到第一条 COMMAND 断言。"""

    def test_base_sandbox_probe_defaults_to_available(self) -> None:
        """内存桩总是可用；probe 刻意不是 abstractmethod，否则已有桩全废。"""
        _FakeSandbox().probe()

    @pytest.mark.parametrize("backend", ["gvisor", "firecracker"])
    def test_real_backends_probe_before_first_command(self, backend: str) -> None:
        """probe 必须真的委托给可用性检测，否则装配期探测形同废纸。

        本机两个后端都不可用（无 runsc / 无 KVM），所以正例就是抛错。
        """
        from ariadne.sandbox_module import SandboxFactory

        sandbox = SandboxFactory(backend)
        with pytest.raises(SandboxUnavailableError):
            sandbox.probe()


class TestWorkerFallbackPolicy:
    """worker 侧的三条分支 —— 决定"没有真隔离时执行还是拒绝"。"""

    @staticmethod
    def _worker(**sandbox_kwargs: object) -> object:
        from ariadne.worker.loop_worker import LoopWorker

        return LoopWorker(settings=Settings(sandbox=SandboxSettings(**sandbox_kwargs)))

    def test_wraps_sandbox_when_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ariadne.worker.loop_worker import build_command_runner

        monkeypatch.setattr(
            "ariadne.sandbox_module.selector.build_sandbox", lambda s: _FakeSandbox()
        )
        runner = build_command_runner(self._worker(profile="standard")._settings)
        assert isinstance(runner, SandboxRunner)
        assert runner.profile is SandboxProfile.STANDARD, "profile 没传下去，档位配置失效"

    def test_returns_none_to_mean_engine_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """None 让 engine 用 RestrictedRunner —— 单机开发的常态，不该报错。"""
        from ariadne.worker.loop_worker import build_command_runner

        monkeypatch.setattr("ariadne.sandbox_module.selector.build_sandbox", lambda s: None)
        runner = build_command_runner(self._worker(fallback_to_restricted=True)._settings)
        assert runner is None

    def test_refuses_to_start_when_fallback_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """显式关掉降级 = 要求"没有真隔离就不执行"。静默降级比启动失败危险。"""
        from ariadne.worker.loop_worker import build_command_runner

        monkeypatch.setattr("ariadne.sandbox_module.selector.build_sandbox", lambda s: None)
        with pytest.raises(RuntimeError, match="拒绝以无隔离状态执行"):
            build_command_runner(self._worker(fallback_to_restricted=False)._settings)

    def test_config_error_is_not_treated_as_unavailable_env(self) -> None:
        """profile 拼错是配置错误，降级会把它掩盖成"环境不满足"。"""
        from ariadne.worker.loop_worker import build_command_runner

        with pytest.raises(ValueError, match="unknown sandbox profile"):
            build_command_runner(
                self._worker(
                    profile="strct", fallback_to_restricted=True
                )._settings
            )


class TestSandboxRunnerSatisfiesProtocol:
    def test_signature_matches_command_runner(self) -> None:
        """签名不合协议，注入进 LoopConfig 会在运行时才炸。"""
        import inspect

        from ariadne.loop_module.verifier.command import CommandRunner

        expected = inspect.signature(CommandRunner.run)
        actual = inspect.signature(SandboxRunner.run)
        assert list(actual.parameters) == list(expected.parameters)
        assert actual.parameters["workdir"].kind is inspect.Parameter.KEYWORD_ONLY

    def test_accepts_injection_into_loop_config(self) -> None:
        from ariadne.loop_module.engine import LoopConfig

        assert "command_runner" in {f.name for f in LoopConfig.__dataclass_fields__.values()}

    def test_runner_forwards_profile_to_sandbox(self) -> None:
        """profile 若没传到 sandbox.run，三档配置就只是个摆设。"""
        seen: list[SandboxProfile] = []

        class _Recording(BaseSandbox):
            async def run(self, cmd, *, workdir, profile=SandboxProfile.STRICT):  # type: ignore[no-untyped-def]
                seen.append(profile)
                from ariadne.loop_module.verifier.restricted_exec import ExecResult

                return ExecResult(exit_code=0, stdout="", stderr="", duration_ms=0)

            async def close(self) -> None:
                return None

        runner = SandboxRunner(sandbox=_Recording(), profile=SandboxProfile.TRUSTED)
        runner.run("echo hi", workdir=Path("."))
        assert seen == [SandboxProfile.TRUSTED]
