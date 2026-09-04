"""RAG 节点 —— 抽象检索。

参数模型：RAGNodeParams（query + top_k + index）。
执行器：RAGNodeExecutor，注入 Retriever Protocol，输出 documents 端口。

Retriever Protocol：
    async def retrieve(self, query: str, *, top_k: int, index: str) -> list[str]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ariadne.graph_module import register_node
from ariadne.graph_module.executor import NodeExecutionContext, NodeExecutor


class Retriever(Protocol):
    """RAG 检索器抽象。"""

    async def retrieve(self, query: str, *, top_k: int, index: str) -> list[str]: ...


@dataclass(frozen=True)
@register_node("rag")
class RAGNodeParams:
    """RAG 节点参数。"""

    query: str
    top_k: int = 5
    index: str = "default"


class RAGNodeExecutor(NodeExecutor):
    """RAG 节点执行器。

    调用注入的 Retriever，输出 {documents: list[str]}。
    """

    def __init__(self, retriever: Retriever) -> None:
        self._retriever = retriever

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        params = ctx.node.params
        query = str(params.get("query", ""))
        # 上游 query 输入优先
        if "query" in ctx.inputs:
            query = str(ctx.inputs["query"])
        top_k = int(params.get("top_k", 5))
        index = str(params.get("index", "default"))
        docs = await self._retriever.retrieve(query, top_k=top_k, index=index)
        return {"documents": list(docs)}


__all__ = [
    "RAGNodeExecutor",
    "RAGNodeParams",
    "Retriever",
]
