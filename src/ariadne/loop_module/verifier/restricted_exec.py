"""受限子进程执行 —— Windows 上的**永久**生产执行路径。

原先标着"M3 过渡方案，M4 换 gVisor"。但本项目只面向 Windows，而
gVisor 与 Firecracker 都是 Linux 技术（前者要 runsc，后者要 KVM），
在 Windows 上永远不可用 —— 所以这里不是过渡层，`sandbox_module` 的
选型逻辑在 Windows 上恒定落到本模块。

受限子进程**无法隔离网络，也防不住内核层逃逸**。它仅适用于
"用户自己的代码在自己的机器上跑"这一场景，不适用于多租户或
不可信代码。因此 `exec.allow_untrusted_code` 默认 False。

做了什么防护：
  - 命令白名单（只认可执行文件名，不接受任意 shell）
  - 不继承环境变量（env 显式构造，避免泄漏 provider API key）
  - 独立临时工作目录，用完删除
  - 硬超时 + 杀整个进程组（子进程 fork 出的孙进程也要清掉）
  - 输出上限，防止日志炸内存
  - CPU / 内存 / 进程数上限：POSIX 用 setrlimit，**Windows 用 Job Object**
    （见 win_job.py。此前 Windows 分支直接返回 None，这三项配了不生效）

做不到什么：
  - 禁网。这是 Windows 上最实质的缺口：进程级无法隔离网络命名空间，
    而 `SandboxProfile.STRICT` 声明的 network_deny 依赖沙箱后端。
    对策只能在上层 —— Harness 的 tool.yaml 规则拦命令串里的出站目标
    （如云元数据地址），而不是靠执行层兜底。
  - 防止 syscall 滥用
  - 防止读取工作目录之外的文件
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

from ariadne.utils.logging import get_logger

if TYPE_CHECKING:
    from ariadne.loop_module.verifier.win_job import WindowsJob

logger = get_logger(__name__)

DEFAULT_TIMEOUT_S: Final = 30
MAX_OUTPUT_BYTES: Final = 1_000_000
DEFAULT_CPU_SECONDS: Final = 30
DEFAULT_MEMORY_BYTES: Final = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_PROCESSES: Final = 64

# 白名单只列**可执行文件名**，参数不限制。
# 刻意不允许 shell / bash / sh / python -c —— 那等于放开任意代码执行。
# npm / npx 的参数另有供应链校验（见 _check_npm_args / _check_npx_args）。
DEFAULT_ALLOWED_COMMANDS: Final[frozenset[str]] = frozenset(
    {
        "pytest",
        "ruff",
        "mypy",
        "python",  # 仅限 -m 形式，见 _check_python_args
        "node",
        "npm",  # 仅限安全子命令 + 脚本白名单，见 _check_npm_args
        "npx",  # 仅限包名白名单，见 _check_npx_args
        "tsc",
        "eslint",
        "vitest",
        "jest",
        "go",
        "cargo",
    }
)

# python 只允许 -m 形式调用已知模块，禁止 -c（任意代码）
_ALLOWED_PYTHON_MODULES: Final[frozenset[str]] = frozenset(
    {"pytest", "ruff", "mypy", "unittest", "coverage"}
)

# npx 允许的包名。npx 会从 registry 下载并执行任意包 —— 放开包名等于
# "从互联网拉一段代码在用户机器上跑"，是红队集点名的供应链缺口
# （prefix-metadata-endpoint 之外仅有的两个 KNOWN_GAP 之一）。
# 只放行本地工具链里常见的运行器；新包要显式进这份清单。
DEFAULT_ALLOWED_NPX_PACKAGES: Final[frozenset[str]] = frozenset(
    {"vitest", "tsc", "typescript", "eslint", "jest", "prettier"}
)

# npm 允许的子命令与 run 脚本名。install/publish/exec 等子命令会下载或
# 外发，一律拒绝；run 的 scripts 内容虽是项目自己的 package.json，但
# 脚本名白名单把"部署/发布类脚本"挡在外面（红队用例 npm-run-arbitrary-script）。
DEFAULT_ALLOWED_NPM_SUBCOMMANDS: Final[frozenset[str]] = frozenset({"test", "run"})
DEFAULT_ALLOWED_NPM_SCRIPTS: Final[frozenset[str]] = frozenset(
    {"build", "test", "lint", "format", "typecheck", "check"}
)

# 环境变量白名单。PATH 必须保留（否则找不到解释器），
# 其余一律不继承 —— 尤其是 *_API_KEY / *_TOKEN。
_ENV_PASSTHROUGH: Final[tuple[str, ...]] = (
    "PATH",
    "SYSTEMROOT",  # Windows 上缺它 subprocess 会失败
    "COMSPEC",
    "TEMP",
    "TMP",
)


class CommandNotAllowedError(ValueError):
    """命令不在白名单内。

    显式报错而非静默拒绝：断言配置写错时用户需要知道原因。
    """


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False
    # 启动失败（找不到可执行文件等），与"命令跑了但失败"区分
    launch_error: str = ""

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.launch_error

    def combined_output(self) -> str:
        parts = [p for p in (self.stderr.strip(), self.stdout.strip()) if p]
        return "\n".join(parts)


@dataclass(frozen=True)
class ExecPolicy:
    """执行策略。默认值全部指向保守。"""

    allowed_commands: frozenset[str] = DEFAULT_ALLOWED_COMMANDS
    # npm / npx 的供应链白名单。空集 = 全部拒绝（比默认更严的部署用）。
    allowed_npx_packages: frozenset[str] = DEFAULT_ALLOWED_NPX_PACKAGES
    allowed_npm_subcommands: frozenset[str] = DEFAULT_ALLOWED_NPM_SUBCOMMANDS
    allowed_npm_scripts: frozenset[str] = DEFAULT_ALLOWED_NPM_SCRIPTS
    timeout_s: int = DEFAULT_TIMEOUT_S
    cpu_seconds: int = DEFAULT_CPU_SECONDS
    memory_bytes: int = DEFAULT_MEMORY_BYTES
    max_processes: int = DEFAULT_MAX_PROCESSES
    max_output_bytes: int = MAX_OUTPUT_BYTES
    # 额外允许的环境变量名。默认空 —— 需要 API key 的命令不该在此执行
    extra_env: dict[str, str] = field(default_factory=dict)


def parse_command(cmd: str) -> list[str]:
    """拆分命令。用 shlex 而非 shell=True。

    shell=True 会让 `pytest; rm -rf /` 这类注入生效，而 shlex 拆分后
    分号只是一个普通参数。
    """
    if not cmd.strip():
        raise CommandNotAllowedError("命令为空")
    # posix=False 在 Windows 上保留反斜杠路径
    return shlex.split(cmd, posix=os.name != "nt")


def _executable_name(argv0: str) -> str:
    """取可执行文件名（去引号、去路径、去 .exe 后缀）。

    去引号是必需的：Windows 上 shlex.split(posix=False) 会保留引号字符，
    而带空格的路径必须加引号（`"C:\\Program Files\\...\\python.exe"`），
    不剥掉的话取出的名字会是 `python.exe"`，白名单校验永远失败。
    """
    cleaned = argv0.strip().strip('"').strip("'")
    name = Path(cleaned).name.lower()
    for suffix in (".exe", ".cmd", ".bat"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def _check_python_args(argv: list[str]) -> None:
    """python 只允许 -m 形式调用白名单模块。

    放开 python 就等于放开任意代码执行（-c 直接跑字符串），
    因此必须限制到具体模块。
    """
    if len(argv) < 3 or argv[1] != "-m":
        raise CommandNotAllowedError(
            "python 仅允许 `python -m <module>` 形式（禁止 -c 等任意代码执行）"
        )
    module = argv[2].split(".")[0]
    if module not in _ALLOWED_PYTHON_MODULES:
        allowed = ", ".join(sorted(_ALLOWED_PYTHON_MODULES))
        raise CommandNotAllowedError(f"python -m {module} 不在白名单内。允许: {allowed}")


def _positionals(argv: list[str]) -> list[str]:
    """取位置参数（跳过选项与其独立取值）。

    独立取值识别靠 _VALUE_FLAGS：npm/npx 的这些选项后面跟的不是子命令/
    包名。识别不全的后果是"把选项值当成了包名做白名单比对" —— 偏严，
    不会放行不该放的命令。
    """
    value_flags = {"-p", "--package", "--registry", "-r", "--prefix", "--cpus"}
    positional: list[str] = []
    skip_next = False
    for arg in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in value_flags:
            skip_next = True
            continue
        if arg.startswith("-"):
            continue
        positional.append(arg)
    return positional


def _package_spec_name(spec: str) -> str:
    """包说明符 → 包名。`vitest@2.1.0` → `vitest`；`@scope/name@x` → `@scope/name`。"""
    if spec.startswith("@"):
        # scoped 包：@scope/name[@version]
        body = spec.split("@", 1)[1]
        return "@" + body.rsplit("@", 1)[0]
    return spec.split("@", 1)[0]


def _check_npx_args(argv: list[str], policy: ExecPolicy) -> None:
    """npx 只允许执行白名单内的包。

    npx 的本质是"从 registry 拉代码并执行"，包名不设限就是给任意远程
    代码执行开门 —— 这正是红队集 npx-arbitrary-package 点名的缺口。
    """
    requested: list[str] = []
    for arg in argv[1:]:
        if arg.startswith("--package="):
            requested.append(arg.split("=", 1)[1])
        elif arg == "--package" or arg == "-p":
            continue  # 独立取值由 _positionals 捕获
    requested.extend(_positionals(argv))

    if not requested:
        return  # `npx --version` 之类，无包可执行

    pkg = _package_spec_name(requested[0])
    if pkg not in policy.allowed_npx_packages:
        allowed = ", ".join(sorted(policy.allowed_npx_packages))
        raise CommandNotAllowedError(
            f"npx 不允许执行包 {pkg!r}（会从 registry 下载并执行任意代码）。"
            f"允许的包: {allowed}"
        )


def _check_npm_args(argv: list[str], policy: ExecPolicy) -> None:
    """npm 只允许白名单子命令；run 的脚本名另设白名单。

    install / exec / publish 等子命令会下载或外发；run 的 scripts 是
    项目自定义的任意 shell —— 脚本名白名单把 deploy/publish 类脚本
    挡住（红队用例 npm-run-arbitrary-script）。
    """
    positional = _positionals(argv)
    if not positional:
        return  # `npm --version` 之类

    subcommand = positional[0]
    if subcommand not in policy.allowed_npm_subcommands:
        allowed = ", ".join(sorted(policy.allowed_npm_subcommands))
        raise CommandNotAllowedError(
            f"npm 子命令 {subcommand!r} 不在白名单内（install/exec/publish 等"
            f"会下载或外发）。允许: {allowed}"
        )
    if subcommand == "run":
        if len(positional) < 2:
            raise CommandNotAllowedError("npm run 缺少脚本名")
        script = positional[1]
        if script not in policy.allowed_npm_scripts:
            allowed = ", ".join(sorted(policy.allowed_npm_scripts))
            raise CommandNotAllowedError(
                f"npm run {script!r} 不在脚本白名单内。允许: {allowed}"
            )


def validate_command(cmd: str, policy: ExecPolicy) -> list[str]:
    """校验并返回 argv。不在白名单则抛 CommandNotAllowedError。

    三层参数校验按可执行文件名分发：python 限 -m 形式，npx 限包名，
    npm 限子命令与脚本名 —— 前两层白名单只看 argv[0]，挡不住
    "白名单前缀 + 危险载荷"（红队集的真正攻击面）。
    """
    argv = parse_command(cmd)
    name = _executable_name(argv[0])

    if name not in policy.allowed_commands:
        allowed = ", ".join(sorted(policy.allowed_commands))
        raise CommandNotAllowedError(
            f"命令 {name!r} 不在白名单内。允许: {allowed}"
        )

    if name == "python":
        _check_python_args(argv)
    elif name == "npx":
        _check_npx_args(argv, policy)
    elif name == "npm":
        _check_npm_args(argv, policy)

    # 剥掉 argv[0] 的引号：Popen 不走 shell，引号会被当成路径的一部分，
    # 导致 FileNotFoundError。
    argv[0] = argv[0].strip().strip('"').strip("'")
    return argv


def _build_env(policy: ExecPolicy) -> dict[str, str]:
    """构造最小环境。

    不用 os.environ.copy()：那会把 ANTHROPIC_API_KEY 之类的凭证
    传进子进程，被执行的代码可以直接读出来。
    """
    env = {
        key: os.environ[key] for key in _ENV_PASSTHROUGH if key in os.environ
    }
    # Resolve bare `python`/`pytest` against the interpreter running Ariadne.
    # On Windows, CreateProcess does not use the child env's PATH while finding
    # the executable, so merely passing the host PATH can silently select a
    # different system Python without the project's dependencies.
    interpreter_dir = str(Path(sys.executable).resolve().parent)
    current_path = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(
        part for part in (interpreter_dir, current_path) if part
    )
    # 让 Python 子进程不写 .pyc、输出不缓冲（超时时能拿到已有输出）
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env.update(policy.extra_env)
    return env


def _resolve_executable(argv0: str, env: dict[str, str]) -> str | None:
    """用**受控 PATH** 把可执行文件名解析成绝对路径。找不到返回 None。

    解决的是一个真实的可用性缺陷：传给子进程的 `env["PATH"]` 并不影响
    Popen 找可执行文件（Windows 上 CreateProcess 读的是调用进程的环境块），
    所以 `python` 会解析到系统安装而非项目 venv —— 用户 venv 里的
    pytest / ruff / mypy 一个都找不到，COMMAND 断言（信号最硬的那类）
    在真实项目里因此不可用。实测：修复前 `python -m pytest` 报
    "No module named pytest"，修复后正常。

    附带把"用哪个二进制"从平台实现细节变成本模块的显式保证。注意
    **当前工作目录劫持并不成立** —— 实测 Windows 上 Popen 不搜索 cwd
    （.bat 与真 .exe 都试过），所以这不是在堵一个已存在的洞，只是不再
    依赖那个未写进契约的平台行为。

    白名单校验在 validate_command 里已按文件名做过，此处只解析路径，
    不放宽任何许可。
    """
    return shutil.which(argv0, path=env.get("PATH"))


def _preexec(policy: ExecPolicy):  # type: ignore[no-untyped-def]
    """POSIX 上的资源限制 + 新进程组。

    Windows 无 resource 模块也无进程组概念，返回 None ——
    这是该方案在 Windows 上防护更弱的地方，已在模块 docstring 说明。
    """
    if sys.platform == "win32":
        return None

    import resource

    def apply() -> None:
        # 独立进程组：超时时能杀掉整棵进程树
        os.setsid()
        resource.setrlimit(
            resource.RLIMIT_CPU, (policy.cpu_seconds, policy.cpu_seconds)
        )
        resource.setrlimit(
            resource.RLIMIT_AS, (policy.memory_bytes, policy.memory_bytes)
        )
        resource.setrlimit(
            resource.RLIMIT_NPROC, (policy.max_processes, policy.max_processes)
        )
        # 禁止产生 core dump（可能含敏感内存内容）
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


def _kill_tree(process: subprocess.Popen[str]) -> None:
    """杀掉整个进程树。

    只 kill 直接子进程不够：pytest 会 fork 出 worker，
    留下的孤儿进程会继续占 CPU。
    """
    if sys.platform == "win32":
        # 先 TerminateProcess 杀直接子进程：内核调用，即时返回。
        # taskkill /T 依赖 WMI/RPC，实测在 WMI 服务异常的机器上会无限
        # 挂起（>10s），把 2s 的超时路径拖到 27s —— 所以它只作为清理
        # 孙进程的尽力而为手段，且必须带短超时。进程树的可靠清杀由
        # Job Object 的 KILL_ON_JOB_CLOSE 兜底（ExitStack 退出时关闭
        # job 句柄，内核直接终止全部成员，不依赖 taskkill 成功）。
        with contextlib.suppress(Exception):
            process.kill()
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=3,
                check=False,
            )
        return

    import signal

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text, False
    head = encoded[: limit // 2].decode("utf-8", errors="ignore")
    tail = encoded[-(limit // 2) :].decode("utf-8", errors="ignore")
    return f"{head}\n…（输出过长已截断）…\n{tail}", True


def _stream_text(value: object) -> str:
    """Normalize subprocess output, including partial bytes from TimeoutExpired."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def run_restricted(
    cmd: str,
    *,
    workdir: Path,
    policy: ExecPolicy | None = None,
) -> ExecResult:
    """在受限子进程中执行命令。

    workdir 必须由调用方准备（通常是 prepare_workspace 的产物）——
    这里不自己建目录，以便调用方控制生命周期与内容注入。
    """
    resolved = policy or ExecPolicy()
    argv = validate_command(cmd, resolved)

    from ariadne.loop_module.verifier.win_job import create_job_for_policy

    with contextlib.ExitStack() as stack:
        job = create_job_for_policy(resolved)
        if job is not None:
            stack.callback(job.close)
        return _spawn_and_collect(argv, workdir=workdir, policy=resolved, job=job)


def _spawn_and_collect(
    argv: list[str],
    *,
    workdir: Path,
    policy: ExecPolicy,
    job: WindowsJob | None,
) -> ExecResult:
    """起进程、纳入 job、收输出。拆出来是为了让 job 的生命周期由调用方的
    ExitStack 管 —— job 必须活过整个 communicate，且退出时关闭以杀净进程树。
    """
    resolved = policy
    start = time.perf_counter()
    env = _build_env(resolved)

    # 显式解析可执行文件：受控 PATH 说了算，不靠平台的搜索顺序
    executable = _resolve_executable(argv[0], env)
    if executable is None:
        return ExecResult(
            exit_code=-1,
            stdout="",
            stderr="",
            duration_ms=int((time.perf_counter() - start) * 1000),
            launch_error=(
                f"在受控 PATH 中找不到可执行文件 {argv[0]!r}"
            ),
        )

    try:
        process = subprocess.Popen(
            [executable, *argv[1:]],
            cwd=str(workdir),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            preexec_fn=_preexec(resolved),
        )
    except (OSError, ValueError) as exc:
        return ExecResult(
            exit_code=-1,
            stdout="",
            stderr="",
            duration_ms=int((time.perf_counter() - start) * 1000),
            launch_error=f"{type(exc).__name__}: {exc}",
        )

    if job is not None:
        from ariadne.loop_module.verifier.win_job import JobObjectError

        try:
            job.assign(int(process._handle))  # type: ignore[attr-defined]
        except JobObjectError as exc:
            logger.warning(
                "进程纳入 Job Object 失败，本次执行无资源上限",
                extra={"cmd": argv[0], "error": str(exc)},
            )

    timed_out = False
    stdout = ""
    stderr = ""
    try:
        stdout, stderr = process.communicate(timeout=resolved.timeout_s)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        # communicate() may have collected a partial stream before timing out.
        # Keep it as a fallback if the post-kill collection cannot complete.
        stdout = _stream_text(exc.stdout or exc.output)
        stderr = _stream_text(exc.stderr)
        _kill_tree(process)
        # 杀完再收一次，拿到超时前已产生的输出
        try:
            collected_stdout, collected_stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # A process tree can outlive taskkill briefly. The command is
            # already classified as timed out; return a bounded result rather
            # than leaking an UnboundLocalError from an unassigned stream.
            _kill_tree(process)
        else:
            stdout = _stream_text(collected_stdout) or stdout
            stderr = _stream_text(collected_stderr) or stderr

    duration_ms = int((time.perf_counter() - start) * 1000)
    stdout, out_cut = _truncate(stdout or "", resolved.max_output_bytes)
    stderr, err_cut = _truncate(stderr or "", resolved.max_output_bytes)

    if timed_out:
        logger.warning(
            "restricted exec timed out",
            extra={"cmd": argv[0], "timeout_s": resolved.timeout_s},
        )

    exit_code = process.returncode if process.returncode is not None else -1
    if job is not None and not timed_out:
        from ariadne.loop_module.verifier.win_job import annotate_quota_kill

        stderr = annotate_quota_kill(stderr, exit_code, cmd=argv[0])

    return ExecResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=duration_ms,
        timed_out=timed_out,
        truncated=out_cut or err_cut,
    )


@contextlib.contextmanager
def prepare_workspace(artifacts: dict[str, str] | None = None):  # type: ignore[no-untyped-def]
    """独立临时工作目录，退出时删除。

    只注入显式给定的 artifact，**不挂载项目其他路径** ——
    被执行的代码看不到源码树的其余部分。
    """
    root = Path(tempfile.mkdtemp(prefix="ariadne-exec-"))
    try:
        for relative, content in (artifacts or {}).items():
            target = root / relative
            # 防目录穿越：解析后必须仍在 root 内
            if not target.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"artifact 路径越界: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)
