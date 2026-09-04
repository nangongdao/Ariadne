"""受限子进程执行的安全边界测试。

这是 M3 的过渡方案（M4 换 gVisor），因此测试要明确划出它**能**防什么
与**不能**防什么 —— 后者写成测试是为了让边界不被误解成"已经安全了"。
"""

from __future__ import annotations

import os
import shlex
import site
import sys
from pathlib import Path

import pytest

from ariadne.loop_module.verifier.restricted_exec import (
    CommandNotAllowedError,
    ExecPolicy,
    parse_command,
    prepare_workspace,
    run_restricted,
    validate_command,
)

# 用解释器的**完整路径**而非裸名字：裸 `python` 会从 PATH 解析到
# 系统 Python（可能没装 pytest），而白名单按文件名判定，
# 完整路径同样通过校验。
PYTHON = shlex.quote(sys.executable) if os.name != "nt" else f'"{sys.executable}"'

# _build_env 刻意剥掉 APPDATA/USERPROFILE（防凭证泄漏），子进程 Python
# 因此定位不到 user site-packages——本机所有包（含 pytest）都装在那里。
# 测试策略经 ExecPolicy.extra_env（设计的扩展点）放行 PYTHONPATH；
# 生产策略不受影响，凭证仍不会进入子进程。
_USER_SITE = site.getusersitepackages()


class TestCommandWhitelist:
    def test_allowed_command_passes(self) -> None:
        argv = validate_command("pytest -q", ExecPolicy())
        assert argv[0] == "pytest"

    def test_unknown_command_rejected(self) -> None:
        with pytest.raises(CommandNotAllowedError, match="不在白名单"):
            validate_command("curl https://evil.com", ExecPolicy())

    @pytest.mark.parametrize("shell", ["sh", "bash", "cmd", "powershell", "zsh"])
    def test_shells_are_not_whitelisted(self, shell: str) -> None:
        """放开 shell 等于放开任意代码执行。"""
        with pytest.raises(CommandNotAllowedError):
            validate_command(f"{shell} -c 'echo hi'", ExecPolicy())

    def test_path_prefix_does_not_bypass(self) -> None:
        """带路径的命令按文件名判定，不能靠 ../ 绕过白名单。"""
        with pytest.raises(CommandNotAllowedError):
            validate_command("/usr/bin/curl http://x", ExecPolicy())

    def test_exe_suffix_normalized(self) -> None:
        argv = validate_command("pytest.exe -q", ExecPolicy())
        assert argv[0].lower().startswith("pytest")

    def test_python_dash_c_rejected(self) -> None:
        """python -c 直接跑字符串 —— 必须禁止。"""
        with pytest.raises(CommandNotAllowedError, match="python -m"):
            validate_command('python -c "import os; os.system(\'rm -rf /\')"', ExecPolicy())

    def test_python_dash_m_unknown_module_rejected(self) -> None:
        with pytest.raises(CommandNotAllowedError, match="不在白名单"):
            validate_command("python -m http.server", ExecPolicy())

    def test_python_dash_m_allowed_module(self) -> None:
        argv = validate_command("python -m pytest -q", ExecPolicy())
        assert argv[:3] == ["python", "-m", "pytest"]

    def test_custom_whitelist_respected(self) -> None:
        policy = ExecPolicy(allowed_commands=frozenset({"echo"}))
        assert validate_command("echo hi", policy)[0] == "echo"
        with pytest.raises(CommandNotAllowedError):
            validate_command("pytest", policy)

    def test_empty_command_rejected(self) -> None:
        with pytest.raises(CommandNotAllowedError, match="为空"):
            validate_command("   ", ExecPolicy())


class TestSupplyChainArgs:
    """npm / npx 的参数级供应链校验（红队用例闭合后的回归钉）。

    白名单只看 argv[0]，挡不住"白名单前缀 + 任意包/脚本"—— npx 会从
    registry 下载并执行任意包，npm install 同理，npm run 执行项目自定义
    的任意 shell。
    """

    # --- npx：包名白名单 ---
    def test_npx_allowlisted_package_passes(self) -> None:
        for cmd in ("npx vitest run", "npx tsc --noEmit", "npx vitest@2.1.0 run"):
            validate_command(cmd, ExecPolicy())

    def test_npx_arbitrary_package_rejected(self) -> None:
        with pytest.raises(CommandNotAllowedError, match="不允许执行包"):
            validate_command("npx some-malicious-package", ExecPolicy())

    def test_npx_version_suffix_stripped_for_match(self) -> None:
        """vitest@latest 按包名 vitest 比对，版本后缀不是混淆手段。"""
        validate_command("npx vitest@latest --version", ExecPolicy())

    def test_npx_scoped_package_rejected_unless_allowlisted(self) -> None:
        with pytest.raises(CommandNotAllowedError):
            validate_command("npx @evil/tracker run", ExecPolicy())

    def test_npx_package_flag_rejected(self) -> None:
        """--package= 也是包来源，不能绕过包名白名单。"""
        with pytest.raises(CommandNotAllowedError):
            validate_command("npx --package=some-malicious-package vitest", ExecPolicy())

    def test_npx_version_only_passes(self) -> None:
        validate_command("npx --version", ExecPolicy())

    # --- npm：子命令白名单 + run 脚本白名单 ---
    def test_npm_safe_subcommands_pass(self) -> None:
        for cmd in ("npm test", "npm --version", "npm run build"):
            validate_command(cmd, ExecPolicy())

    def test_npm_install_rejected(self) -> None:
        with pytest.raises(CommandNotAllowedError, match="不在白名单"):
            validate_command("npm install some-package", ExecPolicy())

    def test_npm_exec_rejected(self) -> None:
        with pytest.raises(CommandNotAllowedError, match="不在白名单"):
            validate_command("npm exec some-package", ExecPolicy())

    def test_npm_run_arbitrary_script_rejected(self) -> None:
        with pytest.raises(CommandNotAllowedError, match="不在脚本白名单"):
            validate_command("npm run deploy-to-prod", ExecPolicy())

    def test_npm_run_without_script_rejected(self) -> None:
        with pytest.raises(CommandNotAllowedError, match="缺少脚本名"):
            validate_command("npm run", ExecPolicy())

    def test_custom_supply_chain_whitelists_respected(self) -> None:
        policy = ExecPolicy(
            allowed_npm_scripts=frozenset({"deploy-to-prod"}),
            allowed_npx_packages=frozenset({"some-malicious-package"}),
        )
        validate_command("npm run deploy-to-prod", policy)
        validate_command("npx some-malicious-package", policy)


class TestInjectionResistance:
    def test_semicolon_is_just_an_argument(self) -> None:
        """不用 shell=True：分号被 shlex 拆成普通参数，不会另起一条命令。"""
        argv = parse_command("pytest -q ; rm -rf /")
        assert ";" in argv
        # 关键：rm 没有变成一个独立命令
        assert argv[0] == "pytest"

    def test_pipe_is_not_interpreted(self) -> None:
        argv = parse_command("pytest | tee out.txt")
        assert argv[0] == "pytest"
        assert "|" in argv

    def test_command_substitution_not_evaluated(self) -> None:
        argv = parse_command("pytest $(whoami)")
        assert "$(whoami)" in argv or "$(whoami)" in " ".join(argv)


class TestWorkspaceIsolation:
    def test_artifacts_written(self) -> None:
        with prepare_workspace({"a.txt": "hello"}) as root:
            assert (root / "a.txt").read_text(encoding="utf-8") == "hello"

    def test_nested_paths_created(self) -> None:
        with prepare_workspace({"pkg/mod.py": "x = 1"}) as root:
            assert (root / "pkg" / "mod.py").is_file()

    def test_path_traversal_rejected(self) -> None:
        """artifact 路径不能逃出工作目录。"""
        with pytest.raises(ValueError, match="越界"), prepare_workspace(
            {"../escaped.txt": "bad"}
        ):
            pass

    def test_workspace_removed_after_exit(self) -> None:
        with prepare_workspace({"a.txt": "x"}) as root:
            captured = root
        assert not captured.exists()

    def test_workspace_removed_on_exception(self) -> None:
        captured: Path | None = None
        with pytest.raises(RuntimeError), prepare_workspace({"a.txt": "x"}) as root:
            captured = root
            raise RuntimeError("boom")
        assert captured is not None
        assert not captured.exists()


@pytest.mark.skipif(
    not Path(sys.executable).is_file(), reason="需要可用的 Python 解释器"
)
class TestActualExecution:
    """真跑子进程。用当前解释器所在目录的 python 作为白名单命令。"""

    @staticmethod
    def policy(**kw: object) -> ExecPolicy:
        base: dict[str, object] = {
            "allowed_commands": frozenset({"python"}),
            "timeout_s": 15,
        }
        if _USER_SITE:
            base["extra_env"] = {"PYTHONPATH": _USER_SITE}
        base.update(kw)
        return ExecPolicy(**base)  # type: ignore[arg-type]

    def test_successful_command(self) -> None:
        with prepare_workspace() as root:
            result = run_restricted(
                f"{PYTHON} -m pytest --version", workdir=root, policy=self.policy()
            )
        assert result.succeeded, result.combined_output()
        assert result.exit_code == 0

    def test_bare_command_resolves_to_controlled_path(self) -> None:
        """裸命令名必须解析到受控 PATH 里的解释器（即项目 venv）。

        这条测的是**用户实际会写的形式**。spec.yaml 里的断言是
        `python -m pytest`，没人会写解释器绝对路径 —— 本文件其余用例
        都用 PYTHON 常量（sys.executable 的完整路径）绕开了这一点，
        于是"裸名字解析到哪"从来没被断言过。

        缺陷现场：传给子进程的 env["PATH"] 不影响 Popen 找可执行文件，
        `python` 因此落到系统安装而非 venv，报 "No module named pytest"
        —— COMMAND 断言在任何用 venv 的真实项目里都不可用。

        **刻意不用 self.policy()**：那个辅助方法把 user site-packages
        注入 PYTHONPATH，系统解释器借此也能 import 到 pytest，缺陷会被
        掩盖（回退验证时实测该 policy 下本用例仍为绿）。生产策略里没有
        这个注入，所以这里用裸 ExecPolicy。
        """
        policy = ExecPolicy(allowed_commands=frozenset({"python"}), timeout_s=15)
        with prepare_workspace() as root:
            result = run_restricted(
                "python -m pytest --version", workdir=root, policy=policy
            )
        assert result.succeeded, (
            f"裸 `python` 没解析到受控 PATH 里的解释器: "
            f"{result.launch_error or result.combined_output()}"
        )
        assert "pytest" in result.stdout.lower()

    def test_missing_executable_reports_launch_error(self) -> None:
        """受控 PATH 里找不到时给明确的 launch_error，而非交给平台去猜。"""
        policy = ExecPolicy(
            allowed_commands=frozenset({"python"}),
            timeout_s=15,
            extra_env={"PATH": ""},
        )
        with prepare_workspace() as root:
            result = run_restricted(
                "python -m pytest --version", workdir=root, policy=policy
            )
        assert not result.succeeded
        assert "找不到可执行文件" in result.launch_error

    def test_failing_command_reports_exit_code(self) -> None:
        with prepare_workspace(
            {"test_fail.py": "def test_x():\n    assert False\n"}
        ) as root:
            result = run_restricted(
                f"{PYTHON} -m pytest -q test_fail.py",
                workdir=root,
                policy=self.policy(),
            )
        assert not result.succeeded
        assert result.exit_code != 0
        assert "assert" in result.combined_output().lower()

    def test_env_does_not_leak_secrets(self) -> None:
        """**关键安全测试**：provider 密钥不能传进子进程。"""
        os.environ["ARIADNE_TEST_FAKE_SECRET"] = "sk-should-not-leak"
        try:
            with prepare_workspace(
                {
                    "test_env.py": (
                        "import os\n"
                        "def test_no_secret():\n"
                        "    assert 'ARIADNE_TEST_FAKE_SECRET' not in os.environ\n"
                    )
                }
            ) as root:
                result = run_restricted(
                    f"{PYTHON} -m pytest -q test_env.py",
                    workdir=root,
                    policy=self.policy(),
                )
            assert result.succeeded, (
                f"密钥泄漏进了子进程环境: {result.combined_output()}"
            )
        finally:
            os.environ.pop("ARIADNE_TEST_FAKE_SECRET", None)

    def test_timeout_kills_process(self) -> None:
        with prepare_workspace(
            {"slow.py": "import time\ntime.sleep(60)\n"}
        ) as root:
            result = run_restricted(
                f"{PYTHON} -m pytest slow.py",
                workdir=root,
                policy=self.policy(timeout_s=2),
            )
        assert result.timed_out
        assert not result.succeeded
        # 超时应在 timeout 附近返回，而非等满 60 秒
        assert result.duration_ms < 20_000

    def test_output_truncated(self) -> None:
        with prepare_workspace(
            {
                "test_loud.py": (
                    "def test_loud():\n"
                    "    for _ in range(2000):\n"
                    "        print('x' * 200)\n"
                    "    assert False\n"
                )
            }
        ) as root:
            result = run_restricted(
                f"{PYTHON} -m pytest -q -s test_loud.py",
                workdir=root,
                policy=self.policy(max_output_bytes=5000),
            )
        assert result.truncated
        assert len(result.stdout.encode()) < 20_000

    def test_launch_error_distinguished_from_failure(self) -> None:
        """找不到可执行文件是环境问题，与"命令跑了但失败"要能区分。"""
        with prepare_workspace() as root:
            result = run_restricted(
                "definitely-not-a-real-binary-xyz --version",
                workdir=root,
                policy=ExecPolicy(
                    allowed_commands=frozenset({"definitely-not-a-real-binary-xyz"})
                ),
            )
        assert result.launch_error
        assert not result.succeeded

    def test_workdir_is_cwd_for_child(self) -> None:
        with prepare_workspace(
            {
                "test_cwd.py": (
                    "import os, pathlib\n"
                    "def test_cwd():\n"
                    "    assert pathlib.Path('marker.txt').is_file()\n"
                ),
                "marker.txt": "here",
            }
        ) as root:
            result = run_restricted(
                f"{PYTHON} -m pytest -q test_cwd.py",
                workdir=root,
                policy=self.policy(),
            )
        assert result.succeeded, result.combined_output()


class TestDocumentedLimitations:
    """把"防不住什么"写成测试，避免边界被误解成"已经安全了"。"""

    def test_network_is_not_blocked(self) -> None:
        """受限子进程**无法**禁网 —— 这是必须等 M4 沙箱的核心原因。

        不实际发起网络请求（测试不该依赖外网），只断言策略里
        没有任何禁网配置项，以此固化该已知缺口。
        """
        policy = ExecPolicy()
        assert not hasattr(policy, "network_disabled")
        assert not hasattr(policy, "allow_network")

    def test_filesystem_outside_workdir_is_reachable(self) -> None:
        """子进程能读工作目录之外的文件 —— 另一个等 M4 的原因。"""
        with prepare_workspace() as root:
            # 工作目录只是 cwd，不是 chroot
            assert root.parent.exists()

    @pytest.mark.skipif(
        sys.platform != "win32", reason="仅说明 Windows 上的额外缺口"
    )
    def test_windows_lacks_resource_limits(self) -> None:
        """Windows 无 resource 模块，CPU/内存限制不生效。

        因此 Windows 上的防护弱于 POSIX，M4 的 gVisor 也只支持 Linux。
        """
        from ariadne.loop_module.verifier.restricted_exec import _preexec

        assert _preexec(ExecPolicy()) is None


class TestQuotedPathHandling:
    """回归测试：带引号的可执行路径。

    Windows 上带空格的路径必须加引号，而 shlex.split(posix=False)
    会保留引号字符。不剥掉的话文件名会变成 `python.exe"`，
    白名单校验永远失败 —— 这个 bug 在 M3 开发中被测试抓到。
    """

    def test_double_quoted_path_validates(self) -> None:
        argv = validate_command(
            '"C:\Program Files\Python311\python.exe" -m pytest',
            ExecPolicy(allowed_commands=frozenset({"python"})),
        )
        # 引号已剥掉，Popen 才能找到文件
        assert not argv[0].startswith('"')
        assert argv[0].endswith("python.exe")

    def test_single_quoted_path_validates(self) -> None:
        argv = validate_command(
            "'/usr/local/bin/pytest' -q",
            ExecPolicy(allowed_commands=frozenset({"pytest"})),
        )
        assert not argv[0].startswith("'")

    def test_quoted_path_still_subject_to_whitelist(self) -> None:
        """剥引号不能变成绕过白名单的手段。"""
        with pytest.raises(CommandNotAllowedError):
            validate_command(
                '"C:\tools\curl.exe" http://evil',
                ExecPolicy(allowed_commands=frozenset({"pytest"})),
            )
