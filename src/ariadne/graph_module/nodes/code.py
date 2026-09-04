"""Code 节点 —— 在沙箱中执行代码。

参数模型：CodeNodeParams（code + language）。
执行器：CodeNodeExecutor，注入 CommandRunner Protocol，输出 result 端口。

CommandRunner 签名（与 loop_module.verifier.command.CommandRunner 一致）：
    def run(self, cmd: str, *, workdir: Path) -> ExecResult

Code 节点把 code 写入临时文件，用 CommandRunner 执行。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ariadne.graph_module import register_node
from ariadne.graph_module.executor import NodeExecutionContext, NodeExecutor

if TYPE_CHECKING:
    from ariadne.loop_module.verifier.restricted_exec import ExecResult


class CodeRunner(Protocol):
    """代码执行器抽象 —— 与 CommandRunner 同构。"""

    def run(self, cmd: str, *, workdir: Path) -> ExecResult: ...


#: 支持的语言 → 受限执行白名单内的可执行文件。只列白名单里能跑的：
#: python（-m 受限）/ node 在 DEFAULT_ALLOWED_COMMANDS 里；julia/go/cargo
#: 等不在，显式拒绝而不是生成一个必失败的命令。
_SUPPORTED_LANGUAGES: frozenset[str] = frozenset({"python", "node", "javascript", "js"})
_LANGUAGE_CMDS: dict[str, str] = {
    "python": "python",
    "node": "node",
    "javascript": "node",
    "js": "node",
}


@dataclass(frozen=True)
@register_node("code")
class CodeNodeParams:
    """Code 节点参数。"""

    code: str
    language: str = "python"
    workdir: str = "."  # 执行工作目录


class CodeNodeExecutor(NodeExecutor):
    """Code 节点执行器。

    用注入的 CodeRunner 执行代码，输出 {result: str}。
    result 是 stdout + stderr 的组合输出。
    """

    def __init__(self, runner: CodeRunner) -> None:
        self._runner = runner

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        import asyncio
        import tempfile

        params = ctx.node.params
        code = str(params.get("code", ""))
        language = str(params.get("language", "python"))
        workdir_str = str(params.get("workdir", "."))

        # 上游 input 注入到 code 的 stdin（作为 __input 变量）
        if "input" in ctx.inputs:
            code = f"__input = {ctx.inputs['input']!r}\n{code}"

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=f".{language}", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            script_path = f.name

        # 只支持白名单语言。`run` 不在受限执行的命令白名单内，且是
        # git 等工具的常见子命令名 —— 用 `run` 执行要么必失败（受限
        # 路径），要么解析到 PATH 上的任意同名程序。映射到白名单内的
        # node：go/cargo 等在受限 runner 下同样不可达，宁可显式报错。
        try:
            language = str(language).lower().strip()
        except AttributeError:
            language = "python"
        if language not in _SUPPORTED_LANGUAGES:
            raise ValueError(
                f"Code 节点语言 {language!r} 不受支持，"
                f"仅支持: {', '.join(sorted(_SUPPORTED_LANGUAGES))}"
            )

        runner_cmd = _LANGUAGE_CMDS[language]
        workdir = Path(workdir_str)
        cmd = f"{runner_cmd} {script_path}"

        # CommandRunner 是同步的，放到线程池执行
        result = await asyncio.to_thread(self._runner.run, cmd, workdir=workdir)

        output = result.combined_output() if hasattr(result, "combined_output") else str(result)
        return {"result": output}


__all__ = [
    "CodeNodeExecutor",
    "CodeNodeParams",
    "CodeRunner",
]
