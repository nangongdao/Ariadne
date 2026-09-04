"""COMMAND 类 Verifier。

信号强度 ★★★★★：退出码是二值且无歧义的。这是为什么代码生成场景
（可用 pytest/tsc/ruff 作断言）是 M3 的标杆场景 —— 反馈信号最硬，
Loop 收敛效率最高。

M3 阶段用受限子进程（见 restricted_exec 的边界说明），M4 换 gVisor 沙箱。
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Protocol

from ariadne.eval_module.base import truncate_evidence
from ariadne.loop_module.goal import Assertion, AssertionKind
from ariadne.loop_module.verifier import register_verifier
from ariadne.loop_module.verifier.base import (
    AssertionOutcome,
    BaseVerifier,
    VerificationContext,
)
from ariadne.loop_module.verifier.restricted_exec import (
    CommandNotAllowedError,
    ExecPolicy,
    ExecResult,
    run_restricted,
)

# 证据截断：一个完整的 pytest 输出能吃掉整个上下文预算
EVIDENCE_HEAD_LINES = 20
EVIDENCE_TAIL_LINES = 5


class CommandRunner(Protocol):
    """命令执行器。

    抽象成 Protocol 让 M4 换沙箱时只替换实现，Verifier 不动；
    单元测试也能用桩，不必真起子进程。
    """

    def run(self, cmd: str, *, workdir: Path) -> ExecResult: ...


class RestrictedRunner:
    """受限子进程执行器（M3 过渡方案）。"""

    def __init__(self, policy: ExecPolicy | None = None) -> None:
        self._policy = policy or ExecPolicy()

    def run(self, cmd: str, *, workdir: Path) -> ExecResult:
        return run_restricted(cmd, workdir=workdir, policy=self._policy)


@register_verifier(AssertionKind.COMMAND)
class CommandVerifier(BaseVerifier):
    """以命令退出码作为断言结果。

    需要 ctx.artifact_path —— 没有工作目录就无处执行，
    这种情况记 errored 而非判失败（是配置问题，不是输出不合格）。
    """

    kind: ClassVar[AssertionKind] = AssertionKind.COMMAND

    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner = runner or RestrictedRunner()

    def _verify(
        self, assertion: Assertion, ctx: VerificationContext
    ) -> AssertionOutcome:
        cmd = str(assertion.spec.get("cmd", ""))
        if not cmd.strip():
            raise ValueError("command 断言的 spec.cmd 不能为空")

        if ctx.artifact_path is None:
            return AssertionOutcome(
                assertion_id=assertion.id,
                kind=assertion.kind,
                passed=False,
                evidence="缺少 artifact_path，无法执行命令（配置问题，非输出不合格）",
                errored=True,
            )

        try:
            result = self._runner.run(cmd, workdir=ctx.artifact_path)
        except CommandNotAllowedError as exc:
            # 白名单拒绝是配置问题：报 errored 让用户去改断言，
            # 判失败会让 critique 给出"修代码"的无效指令
            return AssertionOutcome(
                assertion_id=assertion.id,
                kind=assertion.kind,
                passed=False,
                evidence=str(exc),
                errored=True,
            )

        return AssertionOutcome(
            assertion_id=assertion.id,
            kind=assertion.kind,
            passed=result.succeeded,
            value=1.0 if result.succeeded else 0.0,
            evidence=self._evidence(cmd, result),
            # 启动失败是环境问题，与"命令跑了但失败"区分
            errored=bool(result.launch_error),
            duration_ms=result.duration_ms,
        )

    @staticmethod
    def _evidence(cmd: str, result: ExecResult) -> str:
        if result.succeeded:
            return ""

        if result.launch_error:
            return f"命令启动失败: {result.launch_error}"

        header = (
            f"`{cmd}` 超时（{result.duration_ms}ms）"
            if result.timed_out
            else f"`{cmd}` 退出码 {result.exit_code}"
        )
        output = result.combined_output()
        if not output:
            return header

        return truncate_evidence(
            f"{header}\n{output}",
            head=EVIDENCE_HEAD_LINES,
            tail=EVIDENCE_TAIL_LINES,
        )
