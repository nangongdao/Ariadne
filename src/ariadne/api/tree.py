"""调用树构建。

纯函数，不碰数据库，便于穷尽测试。三个必须处理的现实情况：
1. 父 span 缺失（采样丢弃或还没到）→ 挂为根，不能丢
2. 环（数据异常）→ 检测并断开，否则递归爆栈
3. 多根（一次上报含多条独立链）→ 全部返回
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field


class SpanNode(BaseModel):
    """树节点。self_ms 是排除子节点后的自身耗时，用于定位真正的瓶颈。"""

    span_id: str
    parent_span_id: str
    name: str
    kind: str
    operation: str = ""
    status: str
    error_type: str = ""
    provider: str = ""
    model_request: str = ""
    model_response: str = ""
    started_at: datetime
    duration_ms: int
    self_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: Decimal = Decimal("0")
    input_preview: str = ""
    output_preview: str = ""
    input_ref: str = ""
    output_ref: str = ""
    loop_id: str = ""
    iteration: int = 0
    attributes: dict[str, str] = Field(default_factory=dict)
    tags: list[str] = Field(default_factory=list)
    children: list[SpanNode] = Field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens + self.output_tokens
            + self.cache_read_tokens + self.cache_write_tokens
        )


def _to_node(row: dict[str, Any]) -> SpanNode:
    return SpanNode(
        span_id=str(row["span_id"]),
        parent_span_id=str(row.get("parent_span_id") or ""),
        name=str(row.get("name") or ""),
        kind=str(row.get("kind") or "internal"),
        operation=str(row.get("operation") or ""),
        status=str(row.get("status") or "ok"),
        error_type=str(row.get("error_type") or ""),
        provider=str(row.get("provider") or ""),
        model_request=str(row.get("model_request") or ""),
        model_response=str(row.get("model_response") or ""),
        started_at=row["started_at"],
        duration_ms=int(row.get("duration_ms") or 0),
        input_tokens=int(row.get("input_tokens") or 0),
        output_tokens=int(row.get("output_tokens") or 0),
        cache_read_tokens=int(row.get("cache_read_tokens") or 0),
        cache_write_tokens=int(row.get("cache_write_tokens") or 0),
        reasoning_tokens=int(row.get("reasoning_tokens") or 0),
        cost_usd=Decimal(str(row.get("cost_usd") or "0")),
        input_preview=str(row.get("input_preview") or ""),
        output_preview=str(row.get("output_preview") or ""),
        input_ref=str(row.get("input_ref") or ""),
        output_ref=str(row.get("output_ref") or ""),
        loop_id=str(row.get("loop_id") or ""),
        iteration=int(row.get("iteration") or 0),
        attributes=dict(row.get("attributes") or {}),
        tags=list(row.get("tags") or []),
    )


def build_tree(rows: list[dict[str, Any]]) -> list[SpanNode]:
    """由扁平 span 列表构建调用树，并计算 self_ms。"""
    nodes = {str(r["span_id"]): _to_node(r) for r in rows}
    roots: list[SpanNode] = []

    for node in nodes.values():
        parent = nodes.get(node.parent_span_id) if node.parent_span_id else None
        # 父不存在（缺失或自引用）都当根处理，避免丢 span
        if parent is None or parent.span_id == node.span_id:
            roots.append(node)
        else:
            parent.children.append(node)

    _break_cycles(nodes, roots)

    for root in roots:
        _compute_self_ms(root)
    _sort_recursive(roots)
    return roots


def _break_cycles(nodes: dict[str, SpanNode], roots: list[SpanNode]) -> None:
    """把不可达节点（成环的）提升为根。

    从 roots 出发做可达性遍历；遍历不到的必然在环里。
    """
    reachable: set[str] = set()
    stack = list(roots)
    while stack:
        node = stack.pop()
        if node.span_id in reachable:
            continue
        reachable.add(node.span_id)
        stack.extend(node.children)

    orphans = [n for sid, n in nodes.items() if sid not in reachable]
    for orphan in orphans:
        # 断开入边后提升为根
        parent = nodes.get(orphan.parent_span_id)
        if parent is not None and orphan in parent.children:
            parent.children.remove(orphan)
        orphan.parent_span_id = ""
        roots.append(orphan)
        stack = [orphan]
        while stack:
            node = stack.pop()
            if node.span_id in reachable:
                continue
            reachable.add(node.span_id)
            stack.extend(node.children)


def _compute_self_ms(root: SpanNode) -> None:
    """迭代计算 self_ms，避免深树递归爆栈。"""
    order: list[SpanNode] = []
    stack = [root]
    while stack:
        node = stack.pop()
        order.append(node)
        stack.extend(node.children)

    # 自底向上：子节点耗时之和从自身耗时中扣除
    for node in reversed(order):
        children_ms = sum(c.duration_ms for c in node.children)
        node.self_ms = max(node.duration_ms - children_ms, 0)


def _sort_recursive(roots: list[SpanNode]) -> None:
    roots.sort(key=lambda n: n.started_at)
    stack = list(roots)
    while stack:
        node = stack.pop()
        node.children.sort(key=lambda n: n.started_at)
        stack.extend(node.children)


def flatten_tree(roots: list[SpanNode]) -> list[tuple[int, SpanNode]]:
    """按显示顺序展平为 (depth, node)，供 CLI / 文本渲染使用。

    迭代实现：深链路 trace（agent 递归调用可达上千层）用递归会爆栈。
    """
    output: list[tuple[int, SpanNode]] = []
    stack: list[tuple[SpanNode, int]] = [(r, 0) for r in reversed(roots)]

    while stack:
        node, depth = stack.pop()
        output.append((depth, node))
        # 逆序入栈以保持子节点的原有顺序
        stack.extend((child, depth + 1) for child in reversed(node.children))

    return output
