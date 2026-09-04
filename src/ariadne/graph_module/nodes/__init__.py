"""graph_module.nodes — 八种节点类型。

每种节点类型有：
- 专属参数 dataclass（用 @register_node 注册，供 NodeFactory 按 kind 取）
- 专属 executor（实现 NodeExecutor Protocol）

executor.py 的 GraphExecutor 按 NodeKind 取对应 executor 执行。装配 executor
表用 `graph_module.runtime.build_node_executors`。

此前这里 `__all__` 是空列表，8 个 executor 类只能靠 `from ...nodes.llm import
LLMNodeExecutor` 这种深路径拿到 —— 而 src 里没人这么拿过（唯一构造点是
subgraph.py 的递归自调）。参数 dataclass 有 _NODE_REGISTRY 兜着，executor
两者都没有，于是整层静默死掉。补上导出是让"有哪些 executor"这件事在包边界
上可见。
"""

from __future__ import annotations

from ariadne.graph_module.nodes.branch import BranchNodeExecutor, BranchNodeParams
from ariadne.graph_module.nodes.code import CodeNodeExecutor, CodeNodeParams, CodeRunner
from ariadne.graph_module.nodes.eval import EvalNodeExecutor, EvalNodeParams
from ariadne.graph_module.nodes.llm import LLMNodeExecutor, LLMNodeParams
from ariadne.graph_module.nodes.loop import LoopNodeExecutor, LoopNodeParams
from ariadne.graph_module.nodes.rag import RAGNodeExecutor, RAGNodeParams, Retriever
from ariadne.graph_module.nodes.subgraph import SubgraphNodeExecutor, SubgraphNodeParams
from ariadne.graph_module.nodes.tool import ToolNodeExecutor, ToolNodeParams

__all__ = [
    "BranchNodeExecutor",
    "BranchNodeParams",
    "CodeNodeExecutor",
    "CodeNodeParams",
    "CodeRunner",
    "EvalNodeExecutor",
    "EvalNodeParams",
    "LLMNodeExecutor",
    "LLMNodeParams",
    "LoopNodeExecutor",
    "LoopNodeParams",
    "RAGNodeExecutor",
    "RAGNodeParams",
    "Retriever",
    "SubgraphNodeExecutor",
    "SubgraphNodeParams",
    "ToolNodeExecutor",
    "ToolNodeParams",
]
