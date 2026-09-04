"""沙箱抽象层 —— BaseSandbox + SandboxProfile + SandboxRunner。

沙箱是 M4 替换 M3 受限子进程的关键抽象（docs/M4 §5.4）：
- 用完即毁：不复用实例，避免跨轮次状态残留
- 禁止宿主目录挂载：代码通过 artifact 注入
- 禁网（strict 档）：出站请求失败
- 输出上限：stdout/stderr 各 1MB

SandboxRunner 实现了 loop_module 的 CommandRunner Protocol，
让 CommandVerifier 不改代码就能从受限子进程切到真沙箱。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ariadne.loop_module.verifier.restricted_exec import ExecResult
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)


class SandboxProfile(StrEnum):
    """三档沙箱 profile（docs/04 §6）。

    strict：默认，最高隔离。禁网 + 最小资源。
    standard：中等，允许域名白名单网络。
    trusted：最低隔离，适合用户自己的可信代码。
    """

    STRICT = "strict"
    STANDARD = "standard"
    TRUSTED = "trusted"


class SandboxUnavailableError(RuntimeError):
    """沙箱后端不可用。

    gVisor 需要 Linux 环境 + runsc 运行时。在不可用的环境下实例化
    GVisorSandbox 时抛此错误，调用方可降级到受限子进程（M3 兼容）。
    """


class BaseSandbox(ABC):
    """沙箱抽象。每次 run 创建独立实例，用完即销毁。

    实现类负责：
    - 创建隔离的容器/进程（gVisor / Firecracker）
    - 注入 artifact（代码文件）到沙箱
    - 执行命令并收集输出
    - 执行后销毁实例（不留状态）
    """

    @abstractmethod
    async def run(
        self,
        cmd: str,
        *,
        workdir: Path,
        profile: SandboxProfile = SandboxProfile.STRICT,
    ) -> ExecResult:
        """在沙箱内执行命令。

        workdir 是宿主侧的 artifact 目录 —— 沙箱负责把它注入到隔离环境。
        返回 ExecResult（与受限子进程同构，CommandVerifier 不改代码）。
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """销毁沙箱实例。释放所有资源。"""
        ...

    def probe(self) -> None:  # noqa: B027 — 空实现是刻意的，理由见下
        """装配期可用性探测。不可用时抛 SandboxUnavailableError。

        存在的理由：两个真实后端原本只在 run() 里检测可用性，于是"本机没有
        runsc"这件事要等 Loop 跑到第一条 COMMAND 断言才暴露 —— 那时 LLM
        token 已经烧掉了。装配时先探测，才能在 Loop 开跑前决定降级还是拒绝。

        默认实现为空：内存桩总是可用。真实后端覆盖此方法。
        非抽象方法是刻意的 —— 加 abstractmethod 会让已有的测试桩无法实例化。
        """


@dataclass
class SandboxRunner:
    """CommandRunner Protocol 实现 —— 包装 BaseSandbox 供 CommandVerifier 用。

    CommandRunner 的 run() 是同步的（verifier 调用方是同步的），
    沙箱的 run() 是异步的（IO 密集），这里用事件循环桥接。

    如果已在事件循环内（如 engine 的协程里），用 run_coroutine_threadsafe；
    否则用 asyncio.run。Loop Worker 在协程内调用，需要新线程跑事件循环。
    """

    sandbox: BaseSandbox
    profile: SandboxProfile = SandboxProfile.STRICT

    def run(self, cmd: str, *, workdir: Path) -> ExecResult:
        """同步接口，桥接到异步沙箱。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 不在事件循环里，直接 asyncio.run
            return asyncio.run(self._run_async(cmd, workdir))
        # 已在事件循环里，在新线程跑独立事件循环
        import threading

        result_holder: list[ExecResult | None] = [None]
        error_holder: list[Exception | None] = [None]

        def _run_in_thread() -> None:
            try:
                result_holder[0] = asyncio.run(self._run_async(cmd, workdir))
            except Exception as exc:
                error_holder[0] = exc

        t = threading.Thread(target=_run_in_thread)
        t.start()
        t.join()
        if error_holder[0]:
            raise error_holder[0]
        assert result_holder[0] is not None
        return result_holder[0]

    async def _run_async(self, cmd: str, workdir: Path) -> ExecResult:
        return await self.sandbox.run(cmd, workdir=workdir, profile=self.profile)


__all__ = [
    "BaseSandbox",
    "SandboxProfile",
    "SandboxRunner",
    "SandboxUnavailableError",
]
