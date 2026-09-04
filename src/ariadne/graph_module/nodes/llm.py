"""LLM 节点 —— 调用 LLMClient 生成文本。

参数模型：LLMNodeParams（prompt + model + 可选 temperature/max_tokens）。
执行器：LLMNodeExecutor，注入 LLMClient，输出 text 端口。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ariadne.graph_module import register_node
from ariadne.graph_module.executor import NodeExecutionContext, NodeExecutor

if TYPE_CHECKING:
    from ariadne.loop_module.engine import LLMClient


@dataclass(frozen=True)
@register_node("llm")
class LLMNodeParams:
    """LLM 节点参数。"""

    prompt: str
    model: str
    temperature: float = 0.7
    max_tokens: int = 4096


class LLMNodeExecutor(NodeExecutor):
    """LLM 节点执行器。

    调用 LLMClient.complete() 生成文本，输出 {text: str}。
    """

    def __init__(self, llm: LLMClient, *, default_model: str | None = None) -> None:
        self._llm = llm
        self._default_model = default_model

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        params = ctx.node.params
        prompt = str(params.get("prompt", ""))
        # 如果上游有 prompt 输入，优先用上游数据
        if "prompt" in ctx.inputs:
            prompt = str(ctx.inputs["prompt"])
        model = str(params.get("model") or self._default_model or "")
        if not model:
            raise ValueError("LLM 节点缺少 model 参数，且项目没有默认模型")
        response = await self._llm.complete(prompt, model=model)
        return {"text": response.output}


__all__ = [
    "LLMNodeExecutor",
    "LLMNodeParams",
]
