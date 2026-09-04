"""ariadne rules CLI 测试 —— 离线试跑与压测。

覆盖：
- test 子命令：BLOCK 裁决、放行、退出码语义、非法 hook、坏规则文件
- bench 子命令：压测输出分位数、迭代数下限
- 与 API POST /v1/rules/test 的裁决一致性（同一 evaluator 引擎）

退出码语义：0 成功（BLOCK 也算成功 —— 这是"试跑"不是"跑门禁"）；
2 配置/加载错误。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ariadne.rules_cli import main

SHIPPED_RULES = (
    Path(__file__).resolve().parents[1] / "src" / "ariadne" / "harness_module" / "rules"
)


def _make_rule_file(
    tmp_path: Path,
    *,
    hook: str = "pre_tool",
    when: str = "true",
    action: str = "block",
    rule_id: str = "t",
) -> Path:
    f = tmp_path / "rules.yaml"
    f.write_text(
        f"""
rules:
  - id: {rule_id}
    category: tool
    hook: {hook}
    when: '{when}'
    action: {action}
    message: 命中
""",
        encoding="utf-8",
    )
    return f


class TestTestSubcommand:
    def test_block_decision_printed_and_exit_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """裁决 JSON 应打印到 stdout，且 BLOCK 不是失败（试跑非门禁）。"""
        f = _make_rule_file(tmp_path, rule_id="blocked-rule")
        rc = main(
            [
                "test",
                "--rules", str(f),
                "--hook", "pre_tool",
                "--context", '{"tool": {"cmd": "rm -rf /"}}',
            ]
        )
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["action"] == "block"
        assert out["blocked"] is True
        assert out["winning_hit"]["rule_id"] == "blocked-rule"
        assert "blocked-rule" in out["hits"]

    def test_block_and_allow_both_exit_zero(self, tmp_path: Path) -> None:
        """BLOCK 不是失败 —— 试跑的目的就是看规则拦不拦。"""
        f = _make_rule_file(tmp_path, when="true", action="block")
        assert main(["test", "--rules", str(f), "--hook", "pre_tool"]) == 0

        f2 = _make_rule_file(tmp_path, when="false", action="block", rule_id="t2")
        assert main(["test", "--rules", str(f2), "--hook", "pre_tool"]) == 0

    def test_invalid_hook_returns_2(self, tmp_path: Path) -> None:
        f = _make_rule_file(tmp_path)
        assert main(["test", "--rules", str(f), "--hook", "not_a_hook"]) == 2

    def test_missing_rules_file_returns_2(self, tmp_path: Path) -> None:
        assert main(["test", "--rules", str(tmp_path / "nope.yaml"), "--hook", "pre_tool"]) == 2

    def test_bad_context_json_returns_2(self, tmp_path: Path) -> None:
        f = _make_rule_file(tmp_path)
        assert main(["test", "--rules", str(f), "--hook", "pre_tool", "--context", "{bad"]) == 2

    def test_rule_syntax_error_returns_2(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.yaml"
        f.write_text(
            "rules:\n  - id: x\n    category: tool\n    hook: pre_tool\n"
            "    when: 'not valid cel (('\n",
            encoding="utf-8",
        )
        assert main(["test", "--rules", str(f), "--hook", "pre_tool"]) == 2

    def test_shipped_ruleset_benign_command_allowed(self) -> None:
        """随包规则集下，良性命令必须放行（否则 CLI 一跑就红）。"""
        assert (
            main(
                [
                    "test",
                    "--rules", str(SHIPPED_RULES),
                    "--hook", "pre_tool",
                    "--context", '{"tool": {"cmd": "pytest -q"}}',
                ]
            )
            == 0
        )

    def test_shipped_ruleset_dangerous_command_blocked(self) -> None:
        """随包规则集下，危险命令必须拦截。"""
        assert (
            main(
                [
                    "test",
                    "--rules", str(SHIPPED_RULES),
                    "--hook", "pre_tool",
                    "--context", '{"tool": {"cmd": "sudo rm -rf /"}}',
                ]
            )
            == 0
        )

    def test_empty_context_hint_printed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """空 context 且有命中时，stderr 提示补键（缺键 fail-closed 会误导）。"""
        f = _make_rule_file(tmp_path, hook="pre_persist", rule_id="sens")
        main(["test", "--rules", str(f), "--hook", "pre_persist"])
        err = capsys.readouterr().err
        assert "pre_persist" in err
        assert "--context" in err

    def test_context_given_no_hint(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """给了 context 就不该再提示（用户已补键，噪音无意义）。"""
        f = _make_rule_file(tmp_path, hook="pre_tool", when="false", action="block", rule_id="safe")
        main(
            [
                "test",
                "--rules", str(f),
                "--hook", "pre_tool",
                "--context", '{"tool": {"cmd": "pytest"}}',
            ]
        )
        err = capsys.readouterr().err
        assert "--context" not in err


class TestBenchSubcommand:
    def test_bench_outputs_report(self, tmp_path: Path) -> None:
        f = _make_rule_file(tmp_path, when="true", action="warn")
        rc = main(["bench", "--rules", str(f), "--hook", "pre_tool", "--iterations", "200"])
        assert rc == 0

    def test_iterations_floor(self, tmp_path: Path) -> None:
        """低于下限的迭代数被抬升，压测样本不足时 p99 无意义。"""
        f = _make_rule_file(tmp_path, when="true", action="warn")
        assert main(["bench", "--rules", str(f), "--hook", "pre_tool", "--iterations", "10"]) == 0

    def test_bench_invalid_hook_returns_2(self, tmp_path: Path) -> None:
        f = _make_rule_file(tmp_path)
        assert main(["bench", "--rules", str(f), "--hook", "bad"]) == 2


class TestNoCommand:
    def test_missing_subcommand_fails(self) -> None:
        """没有子命令时 argparse 应报错（缺 required subparsers）。"""
        with pytest.raises(SystemExit) as excinfo:
            main([])
        assert excinfo.value.code == 2
