"""Branch 节点 —— 条件分支路由。

参数模型：BranchNodeParams（condition + branches）。
执行器：BranchNodeExecutor，用受限表达式求值 condition，
返回 {"__route": branch_name} 指定激活的下游分支。

condition 用 AST 白名单求值（见 _safe_eval_condition）：只允许纯表达式节点
（常量、比较、布尔运算、下标、if/else），**拒绝 Call/Attribute/Subscript 之外
的一切对象内省** —— 曾经的 eval(condition, {"__builtins__": {}}) 是伪沙箱，
`().__class__.__mro__[1].__subclasses__()[147].__init__.__globals__['system']
('...')` 这类内省链能绕过空 builtins 直接执行任意代码（安全审查确认）。
AST 白名单从语法层杜绝该逃逸面，而不是依赖运行时的环境限制。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any

from ariadne.graph_module import register_node
from ariadne.graph_module.executor import ROUTE_KEY, NodeExecutionContext, NodeExecutor
from ariadne.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
@register_node("branch")
class BranchNodeParams:
    """Branch 节点参数。

    branches: {branch_name: target_node_id} —— 路由表。
    condition: 表达式，求值结果必须是 branches 的某个 key。
    """

    condition: str
    branches: dict[str, str]


#: 允许的 AST 节点 —— 纯表达式求值所需的所有节点，不含任何可触达对象内省的节点。
#: Name 允许（引用 __input 等求值变量）；Attribute/Call/Subscript 允许但受限：
#:   - Attribute 的 value 必须是 Name（只读 __input.xxx，禁止 __class__ 链）
#:   - Call 参数必须是 Name（`len`/`str` 等内置单参函数）
#:   - Subscript 的 value 必须是 Name（`__input['key']`），slice 必须是常量
_SAFE_AST_NODES: frozenset[type[ast.AST]] = frozenset(
    {
        ast.Expression,
        ast.Constant,
        ast.Name,
        ast.Load,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.Lt,
        ast.LtE,
        ast.Gt,
        ast.GtE,
        ast.In,
        ast.NotIn,
        ast.Is,
        ast.IsNot,
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
        ast.USub,
        ast.UAdd,
        ast.BinOp,
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.Mod,
        ast.Pow,
        ast.FloorDiv,
        ast.IfExp,
        ast.Tuple,
        ast.List,
        ast.Dict,
        ast.Attribute,
        ast.Call,
        ast.Subscript,
        ast.Slice,
    }
)

#: 禁止直接引用的名字 —— 有了这些名，属性链就能触达对象元类。
#: `__class__`/`__mro__`/`__subclasses__` 是经典逃逸链的起点；其余是常用
#: 内省/危险入口，一律拒绝。同时作为 Attribute.attr 黑名单（`__input.__class__`）。
_FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        "__class__",
        "__mro__",
        "__subclasses__",
        "__bases__",
        "__getattribute__",
        "__globals__",
        "__builtins__",
        "globals",
        "getattr",
        "eval",
        "exec",
        "__import__",
        "import",
        "open",
        "input",
    }
)

#: 求值环境提供的受限内置 —— 全部是纯函数，不触达 IO/内省。
#: 只放白名单里的名字进 globals，表达式里出现其他名字一律拒绝。
_SAFE_BUILTINS: dict[str, Any] = {
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
    "sum": sum,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "set": set,
    "enumerate": enumerate,
    "zip": zip,
    "all": all,
    "any": any,
    "sorted": sorted,
}


def _safe_eval_condition(condition: str, eval_ctx: dict[str, Any]) -> Any:
    """AST 白名单求值 condition。语法/白名单违规返回 None（调用方兜底 default）。"""
    try:
        tree = ast.parse(condition, mode="eval")
    except (SyntaxError, ValueError, TypeError):
        logger.warning("branch condition 语法错误，走 default 分支", extra={"condition": condition})
        return None

    if not all(type(node) in _SAFE_AST_NODES for node in ast.walk(tree)):
        logger.warning(
            "branch condition 含白名单外 AST 节点，走 default 分支",
            extra={"condition": condition},
        )
        return None

    # 受限节点再精检：Attribute/Call/Subscript 只允许 Name 打头，杜绝对象内省链
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if not isinstance(node.value, ast.Name):
                return None
            if node.attr in _FORBIDDEN_NAMES:
                return None
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                return None
            if any(not isinstance(a, ast.Name) for a in node.args):
                return None
            if node.keywords:
                return None
        if isinstance(node, ast.Subscript) and not isinstance(node.value, ast.Name):
            return None

    # 名字校验：只允许 __input 与受限内置白名单，其余一律拒绝
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_NAMES:
                return None
            if node.id not in {"__input"} and node.id not in _SAFE_BUILTINS:
                return None

    try:
        compiled = compile(tree, "<branch-condition>", "eval")
        return eval(compiled, {"__builtins__": _SAFE_BUILTINS}, eval_ctx)
    except Exception:
        logger.warning(
            "branch condition 求值失败，走 default 分支",
            extra={"condition": condition},
        )
        return None


class BranchNodeExecutor(NodeExecutor):
    """Branch 节点执行器。

    对 condition 表达式求值，结果作为分支名返回 {ROUTE_KEY: branch_name}。
    求值上下文中的 __input 变量绑定到 ctx.inputs["input"]。
    如果 condition 本身就是字面字符串（在 branches 的 key 中），直接返回。
    """

    async def execute(self, ctx: NodeExecutionContext) -> dict[str, Any]:
        params = ctx.node.params
        condition = str(params.get("condition", ""))
        branches = params.get("branches", {})
        if not isinstance(branches, dict):
            return {ROUTE_KEY: "default"}

        # 如果 condition 是字面分支名，直接路由
        if condition in branches:
            return {ROUTE_KEY: condition}

        # 表达式求值：把 input 绑定为 __input
        eval_ctx: dict[str, Any] = {}
        if "input" in ctx.inputs:
            eval_ctx["__input"] = ctx.inputs["input"]

        result = _safe_eval_condition(condition, eval_ctx)
        if result is None:
            return {ROUTE_KEY: "default"}

        branch_name = str(result)
        if branch_name not in branches:
            return {ROUTE_KEY: "default"}
        return {ROUTE_KEY: branch_name}


__all__ = [
    "BranchNodeExecutor",
    "BranchNodeParams",
]
