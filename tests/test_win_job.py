"""Windows Job Object 资源限制 —— 受限子进程唯一真正强制的那层。

为什么这组测试值得存在：`restricted_exec._preexec` 在 win32 上直接返回 None，
于是 `ExecPolicy` 的 `cpu_seconds` / `memory_bytes` / `max_processes` 三个字段
在 Windows 上**配了但不生效**，只有墙钟超时和输出截断真起作用。而本项目只
面向 Windows，gVisor / Firecracker 都要 Linux，所以受限子进程不是过渡层 ——
它就是生产执行路径，这三个上限失效意味着一个跑飞的 pytest 能吃光内存。

测试策略：每条超限用例都配一条**放宽上限的对照组**。没有对照，"分配失败"
可能只是机器内存本来就不够，证明不了是上限起了作用。

子进程只能用 `python -m <白名单模块>`（`_check_python_args` 的限制），且
`_build_env` 不继承环境变量导致 user site-packages 里的 pytest 不可见，
所以用 stdlib 的 unittest 而非 pytest 当被测载荷。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from ariadne.loop_module.verifier.restricted_exec import ExecPolicy, run_restricted
from ariadne.loop_module.verifier.win_job import (
    STATUS_QUOTA_EXCEEDED,
    JobObjectError,
    WindowsJob,
    describe_exit_code,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Job Object 是 Windows 原生机制")

GB = 1024**3


def _policy(**kwargs: object) -> ExecPolicy:
    """允许 python，其余字段用调用方给的上限。"""
    return ExecPolicy(allowed_commands=("python",), **kwargs)  # type: ignore[arg-type]


def _write_case(workdir: Path, name: str, body: str) -> None:
    """写一个 unittest 模块。body 是 test 方法体，缩进两级。"""
    (workdir / f"{name}.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n    def test_case(self):\n" + body,
        encoding="utf-8",
    )


def _baseline_process_quota(workdir: Path) -> int:
    """探测"跑起一条 python 命令"本身要占几个进程配额。

    不能硬编码 1：`restricted_exec` 现在按受控 PATH 显式解析可执行文件，
    于是 `python` 落到项目 venv 而不是系统安装 —— 而 uv 建的 venv 里
    `python.exe` 是个 trampoline，它要再 CreateProcess 启动 base 解释器，
    自己也占一个配额。硬编码 1 会让命令连启动都做不到（实测 exit=101,
    "Unable to create process"），测试变成在验证"起不来"而不是"起来了但
    spawn 不了"。

    标准 venv / 系统 python 下这里返回 1，uv venv 下返回 2。
    """
    (workdir / "probe.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n"
        "    def test_case(self):\n        print('probe-ok')\n",
        encoding="utf-8",
    )
    for quota in (1, 2, 3, 4):
        result = run_restricted(
            "python -m unittest probe",
            workdir=workdir,
            policy=_policy(max_processes=quota, memory_bytes=GB, timeout_s=60),
        )
        if result.exit_code == 0:
            return quota
    pytest.skip("无法在 4 个进程配额内跑起一条 python 命令，环境异常")


class TestMemoryLimit:
    @pytest.mark.slow
    def test_allocation_beyond_limit_fails(self, tmp_path: Path) -> None:
        _write_case(tmp_path, "test_hog", "        x = bytearray(600*1024*1024)\n")
        result = run_restricted(
            "python -m unittest test_hog",
            workdir=tmp_path,
            policy=_policy(memory_bytes=200 * 1024 * 1024, timeout_s=60),
        )
        assert result.exit_code != 0
        assert "MemoryError" in result.stdout + result.stderr

    @pytest.mark.slow
    def test_same_allocation_succeeds_under_higher_limit(self, tmp_path: Path) -> None:
        """对照组 —— 没有它，上一条证明不了失败是上限造成的。"""
        _write_case(tmp_path, "test_hog", "        x = bytearray(600*1024*1024)\n")
        result = run_restricted(
            "python -m unittest test_hog",
            workdir=tmp_path,
            policy=_policy(memory_bytes=2 * GB, timeout_s=60),
        )
        assert result.exit_code == 0, f"对照组本应成功: {result.stderr[-200:]}"
        assert "MemoryError" not in result.stdout + result.stderr


class TestProcessLimit:
    @pytest.mark.slow
    def test_cannot_spawn_beyond_active_process_limit(self, tmp_path: Path) -> None:
        """fork bomb 的下限防护：配额刚够跑起命令自身，就再起不了任何进程。

        配额用 _baseline_process_quota 探测而非硬编码，理由见该函数
        —— venv 的 python 可能是 trampoline，自己就要占一个配额。
        """
        quota = _baseline_process_quota(tmp_path)
        _write_case(
            tmp_path,
            "test_fork",
            "        import subprocess, sys\n"
            "        spawned = 0\n"
            "        for _ in range(4):\n"
            "            try:\n"
            "                subprocess.Popen([sys.executable, '-c', 'pass'])\n"
            "                spawned += 1\n"
            "            except OSError:\n"
            "                pass\n"
            "        print('spawned', spawned)\n",
        )
        result = run_restricted(
            "python -m unittest test_fork",
            workdir=tmp_path,
            policy=_policy(max_processes=quota, memory_bytes=GB, timeout_s=60),
        )
        assert "spawned 0" in result.stdout, (
            f"子进程没被拦住（配额 {quota}）: {result.stdout!r} {result.stderr[-200:]!r}"
        )

    @pytest.mark.slow
    def test_spawn_succeeds_under_higher_limit(self, tmp_path: Path) -> None:
        """对照组 —— 没有它，上一条证明不了 spawned 0 是配额造成的。

        配额放宽到基线 +4 后，同一段代码必须能起出子进程。
        """
        quota = _baseline_process_quota(tmp_path)
        _write_case(
            tmp_path,
            "test_fork",
            "        import subprocess, sys\n"
            "        spawned = 0\n"
            "        for _ in range(4):\n"
            "            try:\n"
            "                p = subprocess.Popen([sys.executable, '-c', 'pass'])\n"
            "                p.wait()\n"
            "                spawned += 1\n"
            "            except OSError:\n"
            "                pass\n"
            "        print('spawned', spawned)\n",
        )
        result = run_restricted(
            "python -m unittest test_fork",
            workdir=tmp_path,
            policy=_policy(max_processes=quota + 4, memory_bytes=GB, timeout_s=60),
        )
        assert "spawned 0" not in result.stdout, (
            f"对照组本应能起子进程（配额 {quota + 4}）: {result.stdout!r}"
        )


class TestCpuLimit:
    @pytest.mark.slow
    def test_cpu_bound_loop_killed_before_wall_clock_timeout(self, tmp_path: Path) -> None:
        """墙钟给足 60s，所以被杀只能是 CPU 配额 —— timed_out 必须为 False。

        这条断言把两种终止原因分开：若 timed_out 为 True，说明测的是既有的
        墙钟超时路径，Job Object 的 CPU 上限并没有被验证到。
        """
        _write_case(
            tmp_path,
            "test_spin",
            "        import time\n"
            "        t = time.time()\n"
            "        while time.time() - t < 10:\n"
            "            pass\n",
        )
        start = time.monotonic()
        result = run_restricted(
            "python -m unittest test_spin",
            workdir=tmp_path,
            policy=_policy(cpu_seconds=3, timeout_s=60, memory_bytes=GB),
        )
        elapsed = time.monotonic() - start
        assert not result.timed_out, "被墙钟超时杀掉，没验证到 CPU 配额"
        assert result.exit_code & 0xFFFFFFFF == STATUS_QUOTA_EXCEEDED
        # 阈值对标 60s 墙钟而非 10s 自旋时长：CPU 配额是 3s **CPU 时间**，
        # 全量套件并发跑时该进程被调度挤占，烧完 3s CPU 可能花掉 10s 以上
        # 墙钟时间。用 10s 当阈值等于假设"独占 CPU"，是这条曾经的偶发失败原因。
        assert elapsed < 30, f"没有提前终止，耗时 {elapsed:.1f}s"

    @pytest.mark.slow
    def test_quota_kill_is_explained_in_stderr(self, tmp_path: Path) -> None:
        """不翻译退出码，critique 会拿着 3221225540 去猜代码哪里错了。"""
        _write_case(
            tmp_path,
            "test_spin",
            "        import time\n"
            "        t = time.time()\n"
            "        while time.time() - t < 10:\n"
            "            pass\n",
        )
        result = run_restricted(
            "python -m unittest test_spin",
            workdir=tmp_path,
            policy=_policy(cpu_seconds=3, timeout_s=60, memory_bytes=GB),
        )
        assert "[ariadne]" in result.stderr
        assert "资源配额" in result.stderr


class TestNormalCommandsUnaffected:
    @pytest.mark.slow
    def test_passing_test_still_passes(self, tmp_path: Path) -> None:
        """加了 Job Object 不能把正常命令也弄挂 —— 这是回归防线。"""
        _write_case(tmp_path, "test_ok", "        self.assertEqual(6 * 7, 42)\n")
        result = run_restricted(
            "python -m unittest test_ok",
            workdir=tmp_path,
            policy=_policy(memory_bytes=GB, timeout_s=60),
        )
        assert result.exit_code == 0, result.stderr[-200:]
        assert not result.launch_error


class TestJobObjectApi:
    def test_limits_applied_at_construction(self) -> None:
        with WindowsJob(memory_bytes=64 * 1024 * 1024, max_processes=2, cpu_seconds=5) as job:
            assert job is not None

    def test_assign_after_close_raises(self) -> None:
        job = WindowsJob(memory_bytes=GB, max_processes=4, cpu_seconds=5)
        job.close()
        with pytest.raises(JobObjectError, match="已关闭"):
            job.assign(0)

    def test_close_is_idempotent(self) -> None:
        """ExitStack 的 callback 与显式 close 可能都触发，重复关不能炸。"""
        job = WindowsJob(memory_bytes=GB, max_processes=4, cpu_seconds=5)
        job.close()
        job.close()


class TestExitCodeTranslation:
    def test_quota_exceeded_is_described(self) -> None:
        assert describe_exit_code(STATUS_QUOTA_EXCEEDED) is not None
        # subprocess 报的是有符号形式，两者都要认
        assert describe_exit_code(3221225540) is not None

    def test_ordinary_exit_codes_are_not_described(self) -> None:
        """普通失败被误标成"超配额"会把 critique 引向错误方向。"""
        for code in (0, 1, 2, 127, -1):
            assert describe_exit_code(code) is None
