"""Tool 节点 —— 调用工具执行器。

参数模型：ToolNodeParams（cmd + args）。
执行器：ToolNodeExecutor，注入 tool_executor 可调用对象，输出 result 端口。

tool_executor 签名：Callable[[str, dict[str, Any]], Awaitable[str]]
  - 第一个参数是 cmd（工具名）
  - 第二个参数是 args（工具参数）
  - 返回工具执行结果（字符串）
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ariadne.graph_module import register_node
from ariadne.graph_module.executor import NodeExecutionContext, NodeExecutor


@dataclass(frozen=True)
@register_node("tool")
class ToolNodeParams:
    """Tool 节点参数。"""

    cmd: str
    args: dict[str, Any] | None = None  # None 等价于空 dict


class ToolNodeExecutor(NodeExecutor):
    """Tool 节点执行器。

    调用注入的 tool_executor，输出 {result: str}。
    """

    def __init__(
        self,
        tool_executor: Callable[[str, dict[str, Any]], Awaitable[str]],
    ) -> None:
        self._tool_executor = tool_executor

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        params = ctx.node.params
        cmd = str(params.get("cmd", ""))
        args = params.get("args", {})
        if not isinstance(args, dict):
            args = {}
        # 上游 args 输入优先
        if "args" in ctx.inputs and isinstance(ctx.inputs["args"], dict):
            args = {**args, **ctx.inputs["args"]}
        result = await self._tool_executor(cmd, args)
        return {"result": str(result)}


__all__ = [
    "ToolNodeExecutor",
    "ToolNodeParams",
]
