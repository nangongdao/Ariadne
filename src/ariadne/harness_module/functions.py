"""Harness 规则的内置函数库。

宿主侧实现，规则侧只能调用不能定义（docs/04 第 2.2 节）。这保证规则作者
无法用自定义函数绕过沙箱或制造无界求值。

关键安全点：
- matches() 带回溯上限，防 ReDoS（正则灾难性回溯能让规则求值卡死，
  违反 fail-closed 的超时要求）。
- 所有函数纯函数、无副作用、求值时间有界。
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Callable
from typing import Any

from celpy import celtypes

from ariadne.harness_module.injection import detect_injection as _detect_injection
from ariadne.utils.tokens import estimate_tokens as _estimate_tokens

# ReDoS 防护：正则回溯步数上限。超限视为不匹配（fail-closed）。
# 现代正则引擎的回溯是步进式的，可计数；超过阈值说明正则有问题或输入恶意。
MAX_REGEX_STEPS = 100_000
# 正则输入长度上限，避免超大输入拖慢求值
MAX_REGEX_INPUT_CHARS = 200_000


class RegexTimeoutError(Exception):
    """正则回溯超限。ReDoS 防护触发。"""


def regex_match(text: str, pattern: str) -> bool:
    """正则匹配，带回溯上限防 ReDoS。

    命名避开 celpy 内置的 `matches`（CEL 成员方法，签名是 string.matches(pattern)）。
    自定义函数必须用自由函数名，且不能与内置同名 —— 否则 celpy 会尝试把
    第一个参数当 receiver 做方法调用，触发 fail-closed。

    用 re 模块的步数限制不可移植地跨实现，因此用长度上限 + 编译期复杂度
    估算兜底。超限返回 False（fail-closed：宁可漏匹配也不卡死求值）。
    """
    if not isinstance(text, str) or not isinstance(pattern, str):
        return False
    if len(text) > MAX_REGEX_INPUT_CHARS:
        text = text[:MAX_REGEX_INPUT_CHARS]
    try:
        compiled = re.compile(pattern)
    except re.error:
        return False
    if _has_catastrophic_backtracking(compiled):
        # 危险模式直接拒（fail-closed），不尝试求值
        return False
    return bool(compiled.search(text))


def _has_catastrophic_backtracking(pattern: re.Pattern[str]) -> bool:
    """检测灾难性回溯模式。

    简单启发式：嵌套量词（如 `(a+)+`）是 ReDoS 的经典模式。
    完整检测需正则静态分析，M4 用启发式兜底，红队测试集覆盖边界。
    """
    src = pattern.pattern
    # 量词嵌套模式：(a+)+ / (a*)* / (a?)+ —— 闭括号后紧跟量词，且组内有量词
    # 这是 ReDoS 最经典的征兆（指数级回溯）
    return bool(re.search(r"\)[+*?{][+*?{]?", src)) or bool(
        re.search(r"[+*?]\)", src) and re.search(r"\)[+*?]", src)
    )


def count_citations(text: str) -> int:
    """引用计数。支持 Markdown 链接、脚注、[n] 格式。"""
    if not isinstance(text, str):
        return 0
    # Markdown 链接 [text](url)
    links = re.findall(r"\[[^\]]+\]\([^)]+\)", text)
    # 脚注 [^id]
    footnotes = re.findall(r"\[\^[^\]]+\]", text)
    # [n] 数字引用
    numeric = re.findall(r"\[\d+\]", text)
    # 去重：同一引用标记只算一次
    return len(set(links) | set(footnotes) | set(numeric))


def estimate_tokens(payload: str | dict[str, Any]) -> int:
    """预估 Token 数。与 Loop 侧同口径（同一实体 `utils.tokens.estimate_tokens`）。

    粗估而非精确分词：预算判定宁可高估（低估会超支）。

    顶层 import 而非函数内惰性 import：这个函数经 token_count 别名被
    `rules/input.yaml` 与 `rules/output.yaml` 调用，惰性 import 的开销会落在
    **首次规则求值**上。曾经从 loop_module.context 取，那条路径要拖进整个
    loop_module 包（约 3.3s 冷导入），实测首次 evaluate() 757ms、超过
    DEFAULT_TIMEOUT_MS=100 被 fail-closed 判成命中 —— 首个请求无故被拦。
    """
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return _estimate_tokens(text)


def json_valid(text: str, schema: dict[str, Any] | None = None) -> bool:
    """JSON 校验。可选 schema 时用 M2 的评估器。"""
    if not isinstance(text, str):
        return False
    try:
        json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    if schema is None:
        return True
    from ariadne.eval_module import EvaluatorFactory
    from ariadne.eval_module.base import EvalContext

    evaluator = EvaluatorFactory("json_schema", schema=schema)
    result = evaluator.evaluate(EvalContext(item_id="json_valid", output=text))
    return bool(result.passed)


def detect_pii(text: str) -> list[str]:
    """PII 检测。M4 用正则兜底，M6 可接 presidio 增强。

    返回检出的 PII 类型列表（去重）。
    """
    if not isinstance(text, str):
        return []
    found: set[str] = set()
    patterns: dict[str, str] = {
        "email": r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b",
        "phone": r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b",
        "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
        "credit_card": r"\b(?:\d[ -]*?){13,16}\b",
        "ip": r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b",
    }
    for pii_type, pattern in patterns.items():
        if re.search(pattern, text):
            found.add(pii_type)
    return sorted(found)


def detect_injection(text: str) -> list[str]:
    """Prompt injection 检测，返回命中的手法标签。实现见 `injection` 模块。

    顶层 import：与 estimate_tokens 同理，这个函数在 `rules/input.yaml` 的
    pre_model 卡点上被调用，惰性 import 的开销会落在首次规则求值上。
    """
    return _detect_injection(text)


def sensitive_score(text: str) -> float:
    """敏感内容评分 0-1。基于 PII 命中数与密度。"""
    pii = detect_pii(text)
    if not pii:
        return 0.0
    # 类型越多越敏感；密度也计入
    density = min(len(pii) / 10.0, 1.0)
    type_factor = min(len(pii) / 5.0, 1.0)
    return round((type_factor + density) / 2, 2)


def token_count(text: str) -> int:
    """文本 Token 数（与 estimate_tokens 同口径，规则侧别名）。"""
    return estimate_tokens(text)


def _to_cel(value: Any) -> Any:
    """Python 值 → CEL 值。

    celpy 的运算符按 CEL 类型查重载表：`!` / `||` / `&&` 对原生 Python bool
    没有重载，会抛 CELEvalError（"found no matching overload"）。比较运算符
    恰好能容忍原生 int/float，所以 `f(x) > 0` 形式的规则一直是好的，而
    `!f(x)` 和 `f(x) || g(x)` 形式的规则从来没求值成功过 —— 每次都 fail-closed
    成命中，等于无条件拦截。

    bool 必须先于 int 判断：Python 里 bool 是 int 的子类。
    """
    if isinstance(value, bool):
        return celtypes.BoolType(value)
    if isinstance(value, int):
        return celtypes.IntType(value)
    if isinstance(value, float):
        return celtypes.DoubleType(value)
    if isinstance(value, str):
        return celtypes.StringType(value)
    if isinstance(value, (list, tuple)):
        return celtypes.ListType([_to_cel(v) for v in value])
    if isinstance(value, dict):
        return celtypes.MapType(
            {_to_cel(k): _to_cel(v) for k, v in value.items()}
        )
    return value


def _cel_wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
    """把纯 Python 函数包装成返回 CEL 值的 CEL 可调用体。

    刻意做成统一包装而非逐函数手写转换表：新增内置函数时无需记得登记
    返回类型，漏登记不会静默退化成原生类型。
    """

    @functools.wraps(fn)
    def wrapper(*args: Any) -> Any:
        return _to_cel(fn(*args))

    return wrapper


# 规则侧可调用的纯 Python 实现。宿主侧单元测试直接测这些。
_PURE_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "regex_match": regex_match,
    "count_citations": count_citations,
    "estimate_tokens": estimate_tokens,
    "json_valid": json_valid,
    "detect_pii": detect_pii,
    "detect_injection": detect_injection,
    "sensitive_score": sensitive_score,
    "token_count": token_count,
}

# 注册给 CEL 的函数表。键是规则侧可调用的函数名。
# 注意：不能与 celpy 内置函数同名（matches/contains/size/startsWith/endsWith 等），
# 否则 celpy 会按成员方法语义解析，导致 fail-closed 误触发。
BUILTIN_FUNCTIONS: dict[str, Any] = {
    name: _cel_wrap(fn) for name, fn in _PURE_FUNCTIONS.items()
}


__all__ = [
    "BUILTIN_FUNCTIONS",
    "MAX_REGEX_INPUT_CHARS",
    "MAX_REGEX_STEPS",
    "RegexTimeoutError",
    "count_citations",
    "detect_injection",
    "detect_pii",
    "estimate_tokens",
    "json_valid",
    "regex_match",
    "sensitive_score",
    "token_count",
]
