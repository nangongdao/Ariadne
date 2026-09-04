"""DAG 图模型 —— 工作流编排的核心数据结构。

WorkflowGraph 是 M5 编排的基础：一组节点（NodeBase）和边（Edge）构成的
有向无环图。Loop 节点在外部看是单入单出的普通节点，内部是 Goal 驱动的
受控循环 —— 这让 DAG 始终无环，静态环检测、类型推导都能成立。

所有模型用 frozen dataclass（与 Rule/Goal/Assertion 一致），不可变 ——
图加载后不应被运行时修改。序列化由 serialize.py 负责转换 dict ↔ dataclass。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class PortKind(StrEnum):
    """端口数据类型。用于类型兼容校验（验收项 4）。"""

    TEXT = "text"
    DOCUMENTS = "documents"
    JSON = "json"
    ARTIFACT = "artifact"
    ANY = "any"  # 通配，兼容一切


class NodeKind(StrEnum):
    """八种节点类型。"""

    LLM = "llm"
    TOOL = "tool"
    RAG = "rag"
    CODE = "code"
    BRANCH = "branch"
    LOOP = "loop"
    EVAL = "eval"
    SUBGRAPH = "subgraph"


@dataclass(frozen=True)
class Port:
    """节点的输入/输出端口。

    一个端口有名称和类型。边的连接要求源端口类型与目标端口类型兼容。
    """

    name: str
    kind: PortKind = PortKind.ANY
    required: bool = True


@dataclass(frozen=True)
class NodeBase:
    """DAG 节点。不可变。

    每种节点类型（NodeKind）有专属参数存储在 params dict 中。
    参数的校验由 validate.py 按节点类型执行。
    """

    id: str
    kind: NodeKind
    inputs: tuple[Port, ...] = ()
    outputs: tuple[Port, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)

    def input_by_name(self, name: str) -> Port | None:
        for p in self.inputs:
            if p.name == name:
                return p
        return None

    def output_by_name(self, name: str) -> Port | None:
        for p in self.outputs:
            if p.name == name:
                return p
        return None


@dataclass(frozen=True)
class Edge:
    """DAG 边。连接源节点的输出端口到目标节点的输入端口。"""

    source: str  # 源节点 id
    source_port: str
    target: str  # 目标节点 id
    target_port: str


@dataclass(frozen=True)
class WorkflowGraph:
    """完整的工作流图。有向无环图（DAG）。

    Loop 节点内部的循环不算图上的环 —— Loop 是单入单出节点，
    图本身始终无环，因此静态环检测始终有效。
    """

    nodes: tuple[NodeBase, ...] = ()
    edges: tuple[Edge, ...] = ()
    version: str = "1"

    def node_by_id(self, node_id: str) -> NodeBase | None:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None

    @property
    def node_ids(self) -> frozenset[str]:
        return frozenset(n.id for n in self.nodes)

    @property
    def is_empty(self) -> bool:
        return len(self.nodes) == 0


# ---------- 端口类型兼容性 ----------

_COMPAT_MATRIX: dict[PortKind, frozenset[PortKind]] = {
    PortKind.ANY: frozenset(PortKind),  # any 兼容一切
    PortKind.TEXT: frozenset({PortKind.TEXT, PortKind.ANY}),
    PortKind.DOCUMENTS: frozenset({PortKind.DOCUMENTS, PortKind.ANY}),
    PortKind.JSON: frozenset({PortKind.JSON, PortKind.ANY}),
    PortKind.ARTIFACT: frozenset({PortKind.ARTIFACT, PortKind.ANY}),
}


def port_compatible(source: PortKind, target: PortKind) -> bool:
    """源端口类型是否兼容目标端口类型。

    规则（验收项 4）：
    - any 兼容一切
    - 同类型兼容
    - text ↔ json 互不兼容（需显式转换节点）
    """
    return target in _COMPAT_MATRIX.get(source, frozenset())


# ---------- 各节点类型的标准端口定义 ----------

# 便于构造节点的辅助函数。每种节点类型有固定的输入/输出端口契约。

LLM_INPUTS = (Port(name="prompt", kind=PortKind.TEXT),)
LLM_OUTPUTS = (Port(name="text", kind=PortKind.TEXT),)

TOOL_INPUTS = (Port(name="args", kind=PortKind.JSON, required=False),)
TOOL_OUTPUTS = (Port(name="result", kind=PortKind.TEXT),)

RAG_INPUTS = (Port(name="query", kind=PortKind.TEXT),)
RAG_OUTPUTS = (Port(name="documents", kind=PortKind.DOCUMENTS),)

CODE_INPUTS = (Port(name="input", kind=PortKind.ANY, required=False),)
CODE_OUTPUTS = (Port(name="result", kind=PortKind.TEXT),)

BRANCH_INPUTS = (Port(name="input", kind=PortKind.ANY),)
# branch 节点有路由输出端口：边用此端口连接到各分支目标节点。
# 路由选中由 params.branches 决定，执行器据此 skip 未选中的下游。
BRANCH_OUTPUTS = (Port(name="route", kind=PortKind.ANY, required=False),)

LOOP_INPUTS = (Port(name="input", kind=PortKind.ANY, required=False),)
LOOP_OUTPUTS = (
    Port(name="output", kind=PortKind.TEXT),
    Port(name="iterations", kind=PortKind.JSON),
    Port(name="converged", kind=PortKind.JSON),
)

EVAL_INPUTS = (Port(name="input", kind=PortKind.TEXT),)
EVAL_OUTPUTS = (
    Port(name="passed", kind=PortKind.JSON),
    Port(name="verdict", kind=PortKind.TEXT),
)

# subgraph 节点：外部端口是通用桥接（ANY），类型检查在子图内部边上进行。
# params["graph"] 存储嵌套子图的 dict 表示，执行时反序列化为 WorkflowGraph。
SUBGRAPH_INPUTS = (Port(name="input", kind=PortKind.ANY, required=False),)
SUBGRAPH_OUTPUTS = (Port(name="output", kind=PortKind.ANY),)

# 节点类型 → 标准输入端口
NODE_INPUT_PORTS: dict[NodeKind, tuple[Port, ...]] = {
    NodeKind.LLM: LLM_INPUTS,
    NodeKind.TOOL: TOOL_INPUTS,
    NodeKind.RAG: RAG_INPUTS,
    NodeKind.CODE: CODE_INPUTS,
    NodeKind.BRANCH: BRANCH_INPUTS,
    NodeKind.LOOP: LOOP_INPUTS,
    NodeKind.EVAL: EVAL_INPUTS,
    NodeKind.SUBGRAPH: SUBGRAPH_INPUTS,
}

# 节点类型 → 标准输出端口
NODE_OUTPUT_PORTS: dict[NodeKind, tuple[Port, ...]] = {
    NodeKind.LLM: LLM_OUTPUTS,
    NodeKind.TOOL: TOOL_OUTPUTS,
    NodeKind.RAG: RAG_OUTPUTS,
    NodeKind.CODE: CODE_OUTPUTS,
    NodeKind.BRANCH: BRANCH_OUTPUTS,
    NodeKind.LOOP: LOOP_OUTPUTS,
    NodeKind.EVAL: EVAL_OUTPUTS,
    NodeKind.SUBGRAPH: SUBGRAPH_OUTPUTS,
}

# 节点类型 → 必填参数 key
NODE_REQUIRED_PARAMS: dict[NodeKind, frozenset[str]] = {
    NodeKind.LLM: frozenset({"prompt", "model"}),
    NodeKind.TOOL: frozenset({"cmd"}),
    NodeKind.RAG: frozenset({"query"}),
    NodeKind.CODE: frozenset({"code"}),
    NodeKind.BRANCH: frozenset({"condition", "branches"}),
    NodeKind.LOOP: frozenset({"goal"}),
    NodeKind.EVAL: frozenset({"assertions"}),
    NodeKind.SUBGRAPH: frozenset({"graph"}),
}

__all__ = [
    "BRANCH_INPUTS",
    "BRANCH_OUTPUTS",
    "CODE_INPUTS",
    "CODE_OUTPUTS",
    "EVAL_INPUTS",
    "EVAL_OUTPUTS",
    "LLM_INPUTS",
    "LLM_OUTPUTS",
    "LOOP_INPUTS",
    "LOOP_OUTPUTS",
    "NODE_INPUT_PORTS",
    "NODE_OUTPUT_PORTS",
    "NODE_REQUIRED_PARAMS",
    "RAG_INPUTS",
    "RAG_OUTPUTS",
    "SUBGRAPH_INPUTS",
    "SUBGRAPH_OUTPUTS",
    "TOOL_INPUTS",
    "TOOL_OUTPUTS",
    "Edge",
    "NodeBase",
    "NodeKind",
    "Port",
    "PortKind",
    "WorkflowGraph",
    "port_compatible",
]
