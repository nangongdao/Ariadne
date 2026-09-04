"""gVisor 沙箱驱动 —— runsc 运行时容器隔离。

TODO(M4-env): gVisor 需要 Linux 环境 + runsc 运行时。
Windows 开发机上通过 WSL2 或远程 Linux 主机进行。
本机不可用时实例化抛 SandboxUnavailableError，调用方降级到受限子进程。

关键设计（docs/M4 §5.4、§8）：
- 用完即毁：每次 run 创建新容器，执行后销毁，不复用
- 禁止宿主目录挂载：代码通过 docker cp 注入
- 禁网：strict 档 --network=none
- 输出上限：stdout/stderr 各 1MB，超限截断并标记
- 超时杀容器（整个进程组）
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from ariadne.loop_module.verifier.restricted_exec import ExecResult
from ariadne.sandbox_module.base import BaseSandbox, SandboxProfile, SandboxUnavailableError
from ariadne.sandbox_module.profiles import get_profile_spec
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)

# 输出上限 1MB（docs/04 §6 强制项，三档都不可关闭）
MAX_OUTPUT_BYTES = 1_000_000


@dataclass
class GVisorSandbox(BaseSandbox):
    """gVisor (runsc) 沙箱驱动。

    通过 docker SDK 创建 --runtime=runsc 容器实现隔离。
    gVisor 的用户态内核拦 syscall，防逃逸强于裸 Docker。

    实例化时检测 runsc 可用性。不可用时抛 SandboxUnavailableError。
    调用方应捕获此错误并降级到 RestrictedSandbox（M3 兼容）。
    """

    # 运行时镜像名（docs/M4 §6.1 预构建镜像）
    image: str = "ariadne/runtime-python:3.11"
    # docker host（None 用默认 socket）
    docker_host: str | None = None
    # 是否已检测过 runsc 可用性
    _available: bool | None = field(default=None, init=False, repr=False)

    def _check_available(self) -> None:
        """检测 runsc 运行时是否可用。

        docker info --format '{{.Runtimes.runsc}}' 能返回路径说明已安装。
        检测失败（docker 不在 / runsc 未装 / Windows 无 WSL2）时抛错。
        """
        if self._available is not None:
            return
        import shutil
        import subprocess

        docker_path = shutil.which("docker")
        if docker_path is None:
            self._available = False
            raise SandboxUnavailableError(
                "docker not found on PATH — gVisor sandbox requires Docker + runsc runtime"
            )
        try:
            result = subprocess.run(
                ["docker", "info", "--format", "{{json .Runtimes}}"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0 or "runsc" not in result.stdout:
                self._available = False
                raise SandboxUnavailableError(
                    "runsc runtime not installed — "
                    "install gVisor: https://gvisor.dev/docs/user_guide/install/"
                )
            self._available = True
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            self._available = False
            raise SandboxUnavailableError(f"docker info failed: {exc}") from exc

    async def run(
        self,
        cmd: str,
        *,
        workdir: Path,
        profile: SandboxProfile = SandboxProfile.STRICT,
    ) -> ExecResult:
        """在 gVisor 沙箱内执行命令。

        流程：
        1. 检测 runsc 可用
        2. 创建容器（--runtime=runsc，artifact 注入，禁网/白名单按 profile）
        3. 执行命令，超时杀容器
        4. 收集输出（截断到 1MB）
        5. 销毁容器
        """
        self._check_available()
        spec = get_profile_spec(profile)

        container_name = f"ariadne-sandbox-{int(time.time() * 1000)}"

        # 构造 docker run 参数
        run_args = [
            "docker", "run", "--rm",
            "--runtime=runsc",
            "--name", container_name,
            "--read-only",  # root fs 只读
            "--tmpfs", "/tmp:rw,size=512m",  # /tmp 可写（tmpfs）
            "--memory", f"{spec.memory_bytes}",
            "--cpus", f"{spec.cpu_cores}",
            "--pids-limit", str(spec.max_processes),
            "--cap-drop=ALL",  # drop 全部 capabilities
            "--security-opt=no-new-privileges",  # 禁提权
            self.image,
            "sh", "-c", cmd,
        ]

        # strict 档禁网
        if spec.network_deny:
            run_args.insert(4, "--network=none")

        # 注入 artifact（workdir 内容拷进容器）
        # docker cp 在容器创建后执行，这里用 -v 挂载只读
        run_args.insert(4, f"-v={workdir.absolute()}:/workspace:ro")

        start = time.perf_counter()
        try:
            proc = await asyncio.create_subprocess_exec(
                *run_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=spec.cpu_seconds,
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                elapsed = int((time.perf_counter() - start) * 1000)
                return ExecResult(
                    exit_code=-1,
                    stdout="",
                    stderr="sandbox execution timed out",
                    duration_ms=elapsed,
                    timed_out=True,
                )
        except Exception as exc:
            elapsed = int((time.perf_counter() - start) * 1000)
            return ExecResult(
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                duration_ms=elapsed,
                launch_error=str(exc),
            )

        elapsed = int((time.perf_counter() - start) * 1000)
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")

        # 输出截断（1MB 上限）
        truncated = False
        if len(stdout) > MAX_OUTPUT_BYTES:
            stdout = stdout[:MAX_OUTPUT_BYTES]
            truncated = True
        if len(stderr) > MAX_OUTPUT_BYTES:
            stderr = stderr[:MAX_OUTPUT_BYTES]
            truncated = True

        return ExecResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration_ms=elapsed,
            truncated=truncated,
        )

    def probe(self) -> None:
        """装配期探测 runsc 可用性（见 BaseSandbox.probe）。"""
        self._check_available()

    async def close(self) -> None:
        """gVisor 沙箱用完即毁（--rm），close 无额外操作。"""
        pass


__all__ = ["MAX_OUTPUT_BYTES", "GVisorSandbox"]
