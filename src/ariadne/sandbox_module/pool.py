"""沙箱预热池 —— 缓解冷启动延迟。

gVisor 冷启动 ~100-200ms，预热后取用近似 0（docs/M4 §5.4）。
池大小 N = 并发上限 × 1.5，用完即销毁 + 异步补充。

预热池只维护池的"容量"计数，不预创建容器（gVisor 容器是临时的）。
acquire 时如果池有空位则直接创建，否则等待。release 即销毁 + 补充。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ariadne.sandbox_module.base import BaseSandbox, SandboxProfile
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class SandboxPool:
    """沙箱预热池。

    管理沙箱的创建与回收。每次 acquire 返回一个新沙箱（用完即毁模型），
    release 后异步补充池容量。池不持有沙箱实例 —— gVisor 容器用完即毁，
    池只跟踪可用容量。

    warm_factor: 池大小 = max_concurrency × warm_factor（默认 1.5）。
    """

    factory: type[BaseSandbox]
    max_concurrency: int = 4
    warm_factor: float = 1.5
    _available: int = field(default=0, init=False)
    _waiters: list[asyncio.Future[BaseSandbox]] = field(default_factory=list, init=False)
    _closed: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._available = int(self.max_concurrency * self.warm_factor)

    async def acquire(self, profile: SandboxProfile = SandboxProfile.STRICT) -> BaseSandbox:
        """获取一个沙箱。池空时等待。

        返回的沙箱用完后必须调 release —— release 不回收沙箱（用完即毁），
        而是释放池容量让下一个 acquire 不阻塞。
        """
        if self._closed:
            raise RuntimeError("sandbox pool is closed")

        if self._available > 0:
            self._available -= 1
            return self._create_sandbox(profile)

        # 池空，等待
        fut: asyncio.Future[BaseSandbox] = asyncio.get_event_loop().create_future()
        self._waiters.append(fut)
        sandbox = await fut
        # 设置 profile 在创建时决定，这里用调用方传入的
        if sandbox._profile != profile:  # type: ignore[attr-defined]
            await sandbox.close()
            return self._create_sandbox(profile)
        return sandbox

    def _create_sandbox(self, profile: SandboxProfile) -> BaseSandbox:
        sandbox = self.factory()
        sandbox._profile = profile  # type: ignore[attr-defined]
        return sandbox

    async def release(self, sandbox: BaseSandbox) -> None:
        """释放沙箱。销毁实例并补充池容量。"""
        with __import__("contextlib").suppress(Exception):
            await sandbox.close()

        self._available += 1
        if self._waiters:
            waiter = self._waiters.pop(0)
            self._available -= 1
            sandbox = self._create_sandbox(SandboxProfile.STRICT)
            if not waiter.done():
                waiter.set_result(sandbox)

    async def close(self) -> None:
        """关闭池。取消所有等待者。"""
        self._closed = True
        for waiter in self._waiters:
            if not waiter.done():
                waiter.cancel()
        self._waiters.clear()

    @property
    def available(self) -> int:
        """当前可用容量。"""
        return self._available

    @property
    def waiting(self) -> int:
        """等待中的请求数。"""
        return len(self._waiters)


__all__ = ["SandboxPool"]
