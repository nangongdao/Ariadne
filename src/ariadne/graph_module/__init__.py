"""graph_module — DAG 编排的核心模块。

模块清单（docs/M5 §5）：
- models.py — WorkflowGraph / Node / Edge / Port 数据结构
- validate.py — 设计时校验（环检测、类型兼容、必填参数、断言可验证性）
- executor.py — 拓扑排序 + 并发调度 + 条件分支
- serialize.py — ↔ spec.yaml 双向转换
- nodes/ — 八种节点类型的定义与执行
- runtime.py — 按可用依赖装配 executor 表（executor.run 的必需入参）
- importers/ — LangGraph 图定义导入

注册表说明：`_NODE_REGISTRY` 映射 kind → **参数 dataclass**，供设计时校验用；
它不持有 executor。executor 的装配在 runtime.py，因为构造它们要注入
LLMClient / CodeRunner 等外部依赖，注册表拿不到这些。
"""

from __future__ import annotations

from typing import Any

from ariadne.graph_module.models import (
    Edge,
    NodeBase,
    NodeKind,
    Port,
    PortKind,
    WorkflowGraph,
    port_compatible,
)
from ariadne.graph_module.validate import validate_graph

# 节点类型注册表（与 loop_module.modes / eval_module.registry 同构）

_NODE_REGISTRY: dict[str, type] = {}
_loaded = False


def register_node(kind: str) -> Any:
    """注册节点类型的参数模型。

    用法：@register_node("llm") 装饰 LLMNodeParams dataclass。
    """

    def decorator(cls: type) -> type:
        _NODE_REGISTRY[kind] = cls
        return cls

    return decorator


def NodeFactory(kind: str) -> type:  # noqa: N802
    """按 kind 取节点参数模型类。未注册名显式报错。"""
    _ensure_loaded()
    if kind not in _NODE_REGISTRY:
        known = ", ".join(sorted(_NODE_REGISTRY))
        raise ValueError(f"未注册的节点类型 {kind!r}，已支持: {known}")
    return _NODE_REGISTRY[kind]


def available_node_kinds() -> list[str]:
    _ensure_loaded()
    return sorted(_NODE_REGISTRY)


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    # 触发各节点类型的 @register_node 装饰器
    from ariadne.graph_module.nodes import (  # noqa: F401
        branch,
        code,
        llm,
        loop,
        rag,
        subgraph,
        tool,
    )
    from ariadne.graph_module.nodes import (  # noqa: F401
        eval as eval_node,
    )


__all__ = [
    "Edge",
    "NodeBase",
    "NodeFactory",
    "NodeKind",
    "Port",
    "PortKind",
    "WorkflowGraph",
    "available_node_kinds",
    "port_compatible",
    "register_node",
    "validate_graph",
]
