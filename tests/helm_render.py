"""极简 Go template 渲染器，只覆盖 Ariadne chart 实际用到的构造。

存在理由：环境里没有 helm 二进制，而 `{{-` / `-}}` 的空白裁剪语义正是会
静默产出坏 YAML 的地方 —— 注释块少写一个减号，渲染结果就多一行悬空缩进，
`kubectl apply` 才报错。这个渲染器让 chart 的输出能在单测里被 yaml.safe_load。

刻意不做的事：不支持 range、变量赋值、函数管道链、模板继承。
遇到不认识的构造直接抛 UnsupportedConstructError，而不是猜 —— 猜错会让测试
基于错误的渲染结果给出虚假的绿灯。
"""

from __future__ import annotations

import re
from typing import Any

_ACTION = re.compile(r"\{\{(-?)\s*(.*?)\s*(-?)\}\}", re.S)
_DEFINE = re.compile(r'define\s+"([^"]+)"')
_INCLUDE = re.compile(r'include\s+"([^"]+)"\s+(\S+)')
_QUOTE_PIPE = re.compile(r"^(.*?)\s*\|\s*quote$")


class UnsupportedConstructError(RuntimeError):
    """模板里出现了本渲染器不认识的构造。"""


def _resolve(path: str, values: dict[str, Any], dot: Any) -> Any:
    """求值 `.Values.a.b` / `.` / `.Chart.Name` 这类路径表达式。"""
    if path == ".":
        return dot
    if not path.startswith("."):
        raise UnsupportedConstructError(f"不支持的表达式：{path}")
    node: Any = {"Values": values}
    for part in path.lstrip(".").split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _truthy(value: Any) -> bool:
    """Go template 的 falsy：nil / false / 0 / 空字符串 / 空集合。"""
    return bool(value)


def _format(value: Any, quoted: bool) -> str:
    if value is None:
        return ""
    text = ("true" if value else "false") if isinstance(value, bool) else str(value)
    return f'"{text}"' if quoted else text


def _collect_defines(text: str) -> dict[str, str]:
    """抽出所有 define 块的原文（不渲染）。"""
    defines: dict[str, str] = {}
    pos = 0
    while True:
        match = _ACTION.search(text, pos)
        if match is None:
            return defines
        name_match = _DEFINE.match(match.group(2))
        if name_match is None:
            pos = match.end()
            continue
        depth = 1
        body_start = match.end()
        cursor = body_start
        while depth:
            inner = _ACTION.search(text, cursor)
            if inner is None:
                raise UnsupportedConstructError(f'define "{name_match.group(1)}" 没有闭合')
            action = inner.group(2)
            if action.startswith(("define", "if", "with", "range")):
                depth += 1
            elif action.startswith("end"):
                depth -= 1
                if depth == 0:
                    defines[name_match.group(1)] = text[body_start : inner.start()]
                    cursor = inner.end()
                    break
            cursor = inner.end()
        pos = cursor


def render(template: str, values: dict[str, Any], *, defines: dict[str, str] | None = None) -> str:
    """渲染模板片段。defines 用于 include，通常来自 _collect_defines。"""
    defines = defines if defines is not None else {}
    out: list[str] = []
    # 每层记录 (是否输出, 该 if 链是否已有分支命中)
    stack: list[tuple[bool, bool]] = []
    dots: list[Any] = [None]
    pos = 0
    trim_next = False

    def emitting() -> bool:
        return all(active for active, _ in stack)

    while True:
        match = _ACTION.search(template, pos)
        literal = template[pos : match.start()] if match else template[pos:]
        if trim_next:
            literal = literal.lstrip()
            trim_next = False
        if match and match.group(1) == "-":
            literal = literal.rstrip()
        if emitting():
            out.append(literal)
        if match is None:
            return "".join(out)

        trim_next = match.group(3) == "-"
        action = match.group(2)
        pos = match.end()

        if action.startswith("/*"):
            continue

        if action == "end":
            if not stack:
                raise UnsupportedConstructError("多余的 end")
            stack.pop()
            if len(dots) > 1:
                dots.pop()
            continue

        if action.startswith("if "):
            active = emitting() and _truthy(_resolve(action[3:].strip(), values, dots[-1]))
            stack.append((active, active))
            continue

        if action == "else":
            if not stack:
                raise UnsupportedConstructError("else 没有对应的 if")
            _, matched = stack.pop()
            parent_emitting = all(a for a, _ in stack)
            stack.append((parent_emitting and not matched, True))
            continue

        if action.startswith("with "):
            value = _resolve(action[5:].strip(), values, dots[-1])
            active = emitting() and _truthy(value)
            stack.append((active, active))
            dots.append(value)
            continue

        if action.startswith("include"):
            inc = _INCLUDE.match(action)
            if inc is None:
                raise UnsupportedConstructError(action)
            if emitting():
                body = defines.get(inc.group(1))
                if body is None:
                    raise UnsupportedConstructError(f'include 了未定义的模板：{inc.group(1)}')
                out.append(render(body, values, defines=defines))
            continue

        if emitting():
            pipe = _QUOTE_PIPE.match(action)
            expr = pipe.group(1) if pipe else action
            out.append(_format(_resolve(expr, values, dots[-1]), quoted=bool(pipe)))


def render_define(template_text: str, name: str, values: dict[str, Any]) -> str:
    """渲染指定的 define 块。"""
    defines = _collect_defines(template_text)
    if name not in defines:
        raise UnsupportedConstructError(f"没有找到 define {name}，只有 {sorted(defines)}")
    return render(defines[name], values, defines=defines)
