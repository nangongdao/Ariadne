"""Windows Job Object —— 受限子进程在 Windows 上的资源强制层。

存在的理由：`restricted_exec._preexec` 在 win32 上直接返回 None，于是
`ExecPolicy` 的 `cpu_seconds` / `memory_bytes` / `max_processes` 三个字段
**配了但不生效** —— Windows 上只有墙钟超时和输出截断真的起作用。本项目
只面向 Windows，gVisor / Firecracker 都是 Linux 技术，所以受限子进程不是
"M3 过渡方案"而是永久的生产执行路径，它的限制必须真的强制。

Job Object 是 Windows 上与 POSIX setrlimit 对应的原生机制，三项均已实测：
  - ProcessMemoryLimit：128MB 上限下分配 400MB 抛 MemoryError（无 job 时成功）
  - ActiveProcessLimit：上限 2 时，子进程连起 5 个孙进程只成功 1 个
  - PerJobUserTimeLimit：CPU 上限 2s 时，8s 的死循环在 2.6s 墙钟被杀，
    退出码 0xC0000044（STATUS_QUOTA_EXCEEDED）

KILL_ON_JOB_CLOSE 顺带解决了 Windows 上进程树终止不可靠的问题：关掉 job
句柄即杀光整棵树，比 `taskkill /T`（父进程已退出时可能漏掉孙进程）可靠。

用 ctypes 而非 pywin32：不引新依赖，用到的三个 API 签名都很小。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import TYPE_CHECKING

from ariadne.utils.logging import get_logger

if TYPE_CHECKING:
    from ariadne.loop_module.verifier.restricted_exec import ExecPolicy

logger = get_logger(__name__)

# SetInformationJobObject 的 JobObjectInfoClass
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

# JOBOBJECT_BASIC_LIMIT_INFORMATION.LimitFlags
_LIMIT_JOB_TIME = 0x00000004
_LIMIT_ACTIVE_PROCESS = 0x00000008
_LIMIT_PROCESS_MEMORY = 0x00000100
_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

# PerJobUserTimeLimit 的单位是 100 纳秒
_HUNDRED_NS_PER_SECOND = 10_000_000

# 超限终止时的 NTSTATUS —— 用于把退出码翻译成人能看懂的原因
STATUS_QUOTA_EXCEEDED = 0xC0000044
STATUS_NO_MEMORY = 0xC0000017


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class JobObjectError(RuntimeError):
    """Job Object 创建或配置失败。调用方决定是硬失败还是降级放行。"""


def _kernel32() -> ctypes.WinDLL:
    """延迟加载 —— 不在 import 期产生副作用。"""
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return k32


class WindowsJob:
    """带资源上限的 Job Object。上下文管理器，退出即关句柄（连带杀进程树）。

    max_processes 是 job 内**总**进程数，命令自身算一个 —— 上限 1 意味着
    它不能起任何子进程。
    """

    def __init__(self, *, memory_bytes: int, max_processes: int, cpu_seconds: int) -> None:
        self._k32 = _kernel32()
        handle = self._k32.CreateJobObjectW(None, None)
        if not handle:
            raise JobObjectError(f"CreateJobObject 失败，错误码 {ctypes.get_last_error()}")
        self._handle: int | None = handle

        info = _ExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = (
            _LIMIT_PROCESS_MEMORY
            | _LIMIT_ACTIVE_PROCESS
            | _LIMIT_JOB_TIME
            | _LIMIT_KILL_ON_JOB_CLOSE
        )
        info.ProcessMemoryLimit = memory_bytes
        info.BasicLimitInformation.ActiveProcessLimit = max_processes
        info.BasicLimitInformation.PerJobUserTimeLimit = cpu_seconds * _HUNDRED_NS_PER_SECOND
        if not self._k32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            err = ctypes.get_last_error()
            self.close()
            raise JobObjectError(f"SetInformationJobObject 失败，错误码 {err}")

    def assign(self, process_handle: int) -> None:
        """把已创建的进程纳入 job。

        残留竞态：进程在 Popen 返回后就已经在跑，理论上能在本次 assign 之前
        fork 出不受限的孙进程。窗口是进程创建到此调用之间（加载器初始化阶段，
        用户代码还没执行），实测 Python 子进程启动约 30ms 远大于该窗口。
        彻底消除需要 CREATE_SUSPENDED + ResumeThread，而 subprocess 不暴露
        线程句柄 —— 代价是自己重写 CreateProcessW 连管道继承，不值得。
        """
        if self._handle is None:
            raise JobObjectError("job 已关闭")
        if not self._k32.AssignProcessToJobObject(self._handle, process_handle):
            raise JobObjectError(f"AssignProcessToJobObject 失败，错误码 {ctypes.get_last_error()}")

    def close(self) -> None:
        """关句柄。KILL_ON_JOB_CLOSE 会连带终止 job 内所有进程。"""
        if self._handle is not None:
            self._k32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> WindowsJob:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _tb: object,
    ) -> None:
        self.close()


def create_job_for_policy(policy: ExecPolicy) -> WindowsJob | None:
    """按 ExecPolicy 建 job；非 Windows 返回 None（那边走 setrlimit）。

    建不出来时记 warning 并返回 None 而非抛错：`RestrictedRunner` 的定位是
    "用户自己的代码在自己的机器上跑"，为一次 OS 层异常拒绝执行 pytest 的
    代价高于收益。但必须留日志 —— 静默地无限制运行正是要避免的那种缺陷。
    """
    if sys.platform != "win32":
        return None
    try:
        return WindowsJob(
            memory_bytes=policy.memory_bytes,
            max_processes=policy.max_processes,
            cpu_seconds=policy.cpu_seconds,
        )
    except JobObjectError as exc:
        logger.warning(
            "Job Object 创建失败，本次执行无内存/进程数/CPU 上限",
            extra={"error": str(exc)},
        )
        return None


def describe_exit_code(exit_code: int) -> str | None:
    """把超限终止的退出码翻译成原因；不是超限则返回 None。

    没有这层翻译，用户看到的是 3221225540 这种十进制 NTSTATUS。
    """
    unsigned = exit_code & 0xFFFFFFFF
    if unsigned == STATUS_QUOTA_EXCEEDED:
        return "超出资源配额被终止（CPU 时间或进程数上限）"
    if unsigned == STATUS_NO_MEMORY:
        return "超出内存上限被终止"
    return None


def annotate_quota_kill(stderr: str, exit_code: int, *, cmd: str) -> str:
    """超配额被杀时在 stderr 末尾补一句人话；否则原样返回。

    不补的话 critique 拿到的只有 3221225540 这个十进制 NTSTATUS，会去猜代码
    哪里写错了，而真正的原因是资源超限 —— 诊断方向整个跑偏，Loop 会朝错误
    方向改好几轮。
    """
    reason = describe_exit_code(exit_code)
    if reason is None:
        return stderr
    logger.warning(
        "restricted exec 超出资源上限",
        extra={"cmd": cmd, "exit_code": exit_code, "reason": reason},
    )
    return f"{stderr}\n[ariadne] {reason}".strip()


__all__ = [
    "STATUS_NO_MEMORY",
    "STATUS_QUOTA_EXCEEDED",
    "JobObjectError",
    "WindowsJob",
    "annotate_quota_kill",
    "create_job_for_policy",
    "describe_exit_code",
]
